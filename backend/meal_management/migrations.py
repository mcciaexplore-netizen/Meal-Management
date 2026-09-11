import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .database import Session
from .errors import DomainError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    statements: tuple[str, ...]


def sql_statements(source):
    delimiter = ";"
    pending = []
    for line in source.splitlines():
        stripped = line.strip()
        if not pending and stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(maxsplit=1)[1]
            if not delimiter or any(character.isspace() for character in delimiter):
                raise DomainError("INVALID_MIGRATION_DELIMITER")
            continue
        if not stripped and not pending:
            continue
        pending.append(line)
        if stripped.endswith(delimiter):
            statement = "\n".join(pending).strip()[:-len(delimiter)].rstrip()
            if statement:
                yield statement
            pending = []
    if any(line.strip() for line in pending):
        raise DomainError("UNTERMINATED_MIGRATION_STATEMENT")


def load_migrations(directory):
    result = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = re.fullmatch(r"(\d{3})_([a-z0-9_]+)\.sql", path.name)
        if match is None:
            raise DomainError("INVALID_MIGRATION_FILENAME")
        source = path.read_bytes()
        result.append(Migration(
            version=int(match[1]),
            name=match[2],
            checksum=hashlib.sha256(source).hexdigest(),
            statements=tuple(sql_statements(source.decode("utf-8"))),
        ))
    if not result or [item.version for item in result] != list(range(1, len(result) + 1)):
        raise DomainError("MIGRATIONS_MUST_BE_CONTIGUOUS_FROM_ONE")
    if any(not item.statements for item in result):
        raise DomainError("EMPTY_MIGRATION")
    return tuple(result)


def validate_ledger(migrations, rows):
    known = {migration.version: migration for migration in migrations}
    versions = []
    for row in rows:
        migration = known.get(row["version"])
        if migration is None:
            raise DomainError("DATABASE_MIGRATION_UNKNOWN_TO_APPLICATION")
        if row["checksum"] != migration.checksum or row["name"] != migration.name:
            raise DomainError("MIGRATION_CHECKSUM_MISMATCH")
        if row["status"] != "APPLIED":
            raise DomainError("INCOMPLETE_MIGRATION_REQUIRES_MANUAL_REVIEW")
        versions.append(row["version"])
    if sorted(versions) != list(range(1, len(versions) + 1)):
        raise DomainError("MIGRATION_LEDGER_NOT_CONTIGUOUS")
    return tuple(migration for migration in migrations if migration.version not in versions)


def _quote_identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise DomainError("INVALID_SCHEMA_IDENTIFIER")
    return "`" + value + "`"


def _schema_objects(tx):
    return tx.all(
        "SELECT TABLE_NAME AS name, TABLE_TYPE AS type FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
    )


def schema_fingerprint(tx):
    definitions = []
    for item in _schema_objects(tx):
        if item["name"] == "schema_migrations":
            continue
        if item["type"] != "BASE TABLE":
            raise DomainError("BASELINE_UNEXPECTED_SCHEMA_OBJECT")
        row = tx.one("SHOW CREATE TABLE " + _quote_identifier(item["name"]))
        definition = row["Create Table"]
        definition = re.sub(r" AUTO_INCREMENT=\d+", "", definition)
        definitions.append(("TABLE", item["name"], definition))
    triggers = tx.all(
        "SELECT TRIGGER_NAME AS name, ACTION_STATEMENT AS body, ACTION_TIMING AS timing, "
        "EVENT_MANIPULATION AS event, EVENT_OBJECT_TABLE AS table_name "
        "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() "
        "ORDER BY TRIGGER_NAME"
    )
    definitions.extend(
        ("TRIGGER", row["name"], row["timing"], row["event"], row["table_name"], row["body"])
        for row in triggers
    )
    return hashlib.sha256(repr(definitions).encode("utf-8")).hexdigest()


class MigrationRunner:
    def __init__(self, database, directory, through=None):
        self.database = database
        migrations = load_migrations(directory)
        self.migrations = migrations if through is None else tuple(item for item in migrations if item.version <= through)

    def _ledger(self, tx):
        return tx.all(
            "SELECT version, name, checksum, status FROM schema_migrations ORDER BY version"
        )

    def _create_ledger(self, tx):
        tx.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INT UNSIGNED NOT NULL, name VARCHAR(100) NOT NULL, "
            "checksum CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, "
            "status ENUM('APPLYING', 'APPLIED') NOT NULL, "
            "started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), "
            "applied_at DATETIME(6) NULL, PRIMARY KEY (version)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
        )

    def _connect(self):
        connection = self.database._connect()
        try:
            tx = Session(connection)
            version = tx.one("SELECT VERSION() AS version")["version"]
            if not re.match(r"^8\.4(?:\.|$)", version):
                raise DomainError("MIGRATIONS_REQUIRE_MYSQL_8_4")
            schema = tx.one("SELECT DATABASE() AS name")["name"]
            if not schema:
                raise DomainError("DATABASE_NOT_SELECTED")
            lock_name = "meal-migrations-" + hashlib.sha256(schema.encode()).hexdigest()[:40]
            acquired = tx.one("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
            if not acquired or acquired["acquired"] != 1:
                raise DomainError("MIGRATIONS_ALREADY_RUNNING")
            return connection, tx, lock_name
        except BaseException:
            connection.close()
            raise

    def _close(self, connection, tx, lock_name):
        try:
            tx.one("SELECT RELEASE_LOCK(%s) AS released", (lock_name,))
        finally:
            connection.close()

    def inspect(self):
        connection, tx, lock_name = self._connect()
        try:
            objects = _schema_objects(tx)
            return {
                "fingerprint": schema_fingerprint(tx),
                "tables": [item["name"] for item in objects],
                "migrations": self._ledger(tx) if any(item["name"] == "schema_migrations" for item in objects) else [],
            }
        finally:
            self._close(connection, tx, lock_name)

    def baseline(self, version, expected_fingerprint):
        if version != 1 or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint or ""):
            raise DomainError("BASELINE_REQUIRES_VERSION_ONE_AND_REVIEWED_FINGERPRINT")
        connection, tx, lock_name = self._connect()
        try:
            objects = _schema_objects(tx)
            if any(item["name"] == "schema_migrations" for item in objects):
                raise DomainError("BASELINE_REQUIRES_UNTRACKED_DATABASE")
            expected_tables = {
                re.match(r"CREATE TABLE ([A-Za-z0-9_]+)", statement)[1]
                for statement in self.migrations[0].statements
                if statement.startswith("CREATE TABLE ")
            }
            if {item["name"] for item in objects} != expected_tables:
                raise DomainError("BASELINE_SCHEMA_TABLES_DO_NOT_MATCH")
            if schema_fingerprint(tx) != expected_fingerprint:
                raise DomainError("BASELINE_SCHEMA_FINGERPRINT_CHANGED")
            self._create_ledger(tx)
            migration = self.migrations[0]
            tx.execute(
                "INSERT INTO schema_migrations (version, name, checksum, status, applied_at) "
                "VALUES (%s, %s, %s, 'APPLIED', UTC_TIMESTAMP(6))",
                (migration.version, migration.name, migration.checksum),
            )
            connection.commit()
            return migration.version
        finally:
            self._close(connection, tx, lock_name)

    def apply(self):
        connection, tx, lock_name = self._connect()
        try:
            objects = _schema_objects(tx)
            tracked = any(item["name"] == "schema_migrations" for item in objects)
            if objects and not tracked:
                raise DomainError("UNTRACKED_DATABASE_REQUIRES_REVIEWED_BASELINE")
            if not tracked:
                self._create_ledger(tx)
                connection.commit()
            pending = validate_ledger(self.migrations, self._ledger(tx))
            applied = []
            for migration in pending:
                tx.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, status) "
                    "VALUES (%s, %s, %s, 'APPLYING')",
                    (migration.version, migration.name, migration.checksum),
                )
                connection.commit()
                for statement in migration.statements:
                    tx.execute(statement)
                tx.execute(
                    "UPDATE schema_migrations SET status = 'APPLIED', applied_at = UTC_TIMESTAMP(6) "
                    "WHERE version = %s AND checksum = %s",
                    (migration.version, migration.checksum),
                )
                connection.commit()
                applied.append(migration.version)
            return tuple(applied)
        finally:
            self._close(connection, tx, lock_name)
