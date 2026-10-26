import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .auth import audit
from .database import Database, Session
from .errors import DomainError
from .migrations import _quote_identifier, schema_fingerprint
from .security import normalize_email


WIPE_TABLES = (
    "meal_type_corrections", "meal_voids", "meals", "servings", "scan_attempts", "scan_app_requests",
    "visitor_authorizations", "serving_requests", "master_qr_meal_types", "master_qr_allocations",
    "email_queue", "employee_email_batches", "development_seed_records", "development_seed_batches",
    "qr_credentials", "employee_meal_types", "employee_archives", "employees", "waiter_shifts",
    "audit_events", "staff_sessions", "login_rate_limits",
)
KEEP_TABLES = (
    "staff_accounts", "staff_account_roles", "roles", "meal_types", "departments", "locations",
    "scanner_devices", "scan_app_settings", "waiter_shift_settings", "system_locks", "schema_migrations",
)
PHONE_TABLE = "scanner_phones"
FRESH_COUNTER_TABLES = ("audit_events",)
REQUIRED_MIGRATIONS = tuple(range(1, 15))
RUNTIME_USER = "meal_runtime"
SNAPSHOT_ID = re.compile(r"(?:rds:)?[A-Za-z](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,254}")
PHOTO_KEY = re.compile(r"(?:shift-selfies/)?[0-9a-f]{64}\.(?:jpg|png)")
STATE_FILE = "reset-state.json"
RUN_ORDER = (
    "1. docker compose --env-file .env.docker stop admin scanner",
    "2. Dry run (this command without --confirm)",
    "3. Real run (--confirm --snapshot-id <id> [--include-s3])",
    "4. docker compose --env-file .env.docker up -d",
    "5. Health checks: https://<admin host>/health/ready and https://<scanner host>/health/ready",
)


def confirmation_text(database, host):
    return "WIPE " + database + " ON " + host


def parse_phone_ids(value):
    if value is None or not value.strip():
        return ()
    identifiers = []
    for part in value.split(","):
        part = part.strip()
        if not re.fullmatch(r"[1-9][0-9]{0,19}", part):
            raise DomainError("INVALID_KEEP_PHONES")
        identifiers.append(int(part))
    if len(set(identifiers)) != len(identifiers):
        raise DomainError("INVALID_KEEP_PHONES")
    return tuple(sorted(identifiers))


class TestDataReset:
    def __init__(self, settings, expect_host, expect_db, operator_email, keep_phones=(), s3_client=None,
                 s3_bucket=None, output=print, truncate_hook=None):
        self.settings = settings
        self.expect_host = expect_host
        self.expect_db = expect_db
        self.operator_email = normalize_email(operator_email)
        self.keep_phones = tuple(keep_phones)
        self.s3 = s3_client
        self.bucket = s3_bucket
        self.output = output
        self.truncate_hook = truncate_hook

    def _connect(self):
        if self.settings.db_host != self.expect_host:
            raise DomainError("RESET_HOST_MISMATCH")
        if self.settings.db_name != self.expect_db:
            raise DomainError("RESET_DATABASE_MISMATCH")
        connection = Database(self.settings)._connect()
        return connection, Session(connection)

    def _preflight(self, tx):
        server = tx.one("SELECT VERSION() AS version, DATABASE() AS name, CURRENT_USER() AS user")
        if not re.match(r"^8\.4(?:\.|$)", server["version"]):
            raise DomainError("RESET_REQUIRES_MYSQL_8_4")
        if server["name"] != self.expect_db:
            raise DomainError("RESET_DATABASE_MISMATCH")
        user, _, host = server["user"].rpartition("@")
        if user == RUNTIME_USER:
            raise DomainError("RESET_REQUIRES_ADMIN_ACCOUNT")
        grantee = "'" + user + "'@'" + host + "'"
        can_drop = tx.one(
            "SELECT (EXISTS (SELECT 1 FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = %s AND PRIVILEGE_TYPE = 'DROP') "
            "OR EXISTS (SELECT 1 FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE = %s AND PRIVILEGE_TYPE = 'DROP' "
            "AND %s LIKE TABLE_SCHEMA)) AS allowed",
            (grantee, grantee, self.expect_db),
        )
        if not can_drop or not can_drop["allowed"]:
            raise DomainError("RESET_REQUIRES_ADMIN_ACCOUNT")
        ledger = tx.all("SELECT version, status FROM schema_migrations ORDER BY version")
        if tuple(row["version"] for row in ledger) != REQUIRED_MIGRATIONS or any(row["status"] != "APPLIED" for row in ledger):
            raise DomainError("RESET_REQUIRES_MIGRATIONS_ONE_TO_FOURTEEN")
        operator = tx.one(
            "SELECT s.id FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.email = %s AND s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN'",
            (self.operator_email,),
        )
        if operator is None:
            raise DomainError("RESET_OPERATOR_MUST_BE_ACTIVE_ADMIN")
        phones = tx.all(
            "SELECT id, label, source, activated_at, active_until, revoked_at FROM scanner_phones ORDER BY id"
        )
        by_id = {row["id"]: row for row in phones}
        for identifier in self.keep_phones:
            row = by_id.get(identifier)
            if row is None or row["revoked_at"] is not None or row["activated_at"] is None:
                raise DomainError("RESET_KEEP_PHONE_NOT_ACTIVE")
        return {"server": server, "operator_id": operator["id"], "phones": phones}

    def _state(self, tx):
        tx.execute("SET SESSION information_schema_stats_expiry = 0")
        tables = {row["name"] for row in tx.all(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        )}
        expected = set(WIPE_TABLES) | set(KEEP_TABLES) | {PHONE_TABLE}
        if tables != expected:
            raise DomainError("RESET_UNEXPECTED_TABLES")
        counts = {name: tx.one("SELECT COUNT(*) AS count FROM " + _quote_identifier(name))["count"] for name in sorted(tables)}
        checksums = {}
        for name in KEEP_TABLES:
            row = tx.one("CHECKSUM TABLE " + _quote_identifier(name) + " EXTENDED")
            checksums[name] = row["Checksum"]
        counters = {row["name"]: row["next_id"] for row in tx.all(
            "SELECT TABLE_NAME AS name, AUTO_INCREMENT AS next_id FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND AUTO_INCREMENT IS NOT NULL"
        )}
        guards = {
            "triggers": tx.all(
                "SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_ORDER, ACTION_STATEMENT, "
                "ACTION_TIMING, CREATED, SQL_MODE, DEFINER FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() ORDER BY TRIGGER_NAME"
            ),
            "checks": tx.all(
                "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() ORDER BY CONSTRAINT_NAME"
            ),
            "constraints": tx.all(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, ENFORCED FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() ORDER BY TABLE_NAME, CONSTRAINT_NAME"
            ),
            "foreign_keys": tx.all(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME, UPDATE_RULE, DELETE_RULE "
                "FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() "
                "ORDER BY TABLE_NAME, CONSTRAINT_NAME"
            ),
            "fingerprint": schema_fingerprint(tx),
        }
        return {"counts": counts, "checksums": checksums, "counters": counters, "guards": guards}

    def _photo_keys(self, tx):
        rows = tx.all(
            "SELECT selfie_object_key AS object_key FROM employees WHERE selfie_object_key IS NOT NULL "
            "UNION SELECT selfie_object_key FROM waiter_shifts"
        )
        keys = sorted({row["object_key"] for row in rows})
        return [key for key in keys if PHOTO_KEY.fullmatch(key)], [key for key in keys if not PHOTO_KEY.fullmatch(key)]

    def _s3_report(self, keys):
        if self.s3 is None or not self.bucket:
            self.output("S3: no bucket given; S3 objects are not checked.")
            return
        selfies = sum(key.startswith("shift-selfies/") for key in keys)
        self.output(f"S3 bucket {self.bucket}: {len(keys) - selfies} employee photos and {selfies} shift selfies are recorded in the database.")
        try:
            status = self.s3.get_bucket_versioning(Bucket=self.bucket).get("Status") or "Never enabled"
            self.output("S3 versioning: " + status + ("" if status == "Never enabled" else
                        ". Deleting adds delete markers; earlier versions stay until removed in the console or by a lifecycle rule."))
        except Exception:
            self.output("S3 versioning: not readable with this role. Check Properties > Bucket Versioning in the S3 console.")
        try:
            listed = {"photos": 0, "selfies": 0}
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    listed["selfies" if item["Key"].startswith("shift-selfies/") else "photos"] += 1
            self.output(f"S3 listing: {listed['photos']} objects outside shift-selfies/ and {listed['selfies']} under shift-selfies/.")
        except Exception:
            self.output("S3 listing: not permitted for this role; only keys recorded in the database will be deleted.")

    def _print_state(self, preflight, state, keys, rejected):
        server = preflight["server"]
        self.output(f"Target: {self.settings.db_host}/{server['name']} as {server['user']} (MySQL {server['version']})")
        self.output("Tables to wipe:")
        for name in WIPE_TABLES:
            self.output(f"  {name}: {state['counts'][name]} rows")
        self.output("Tables kept unchanged:")
        for name in KEEP_TABLES:
            self.output(f"  {name}: {state['counts'][name]} rows")
        self.output("Scanner phones and codes:")
        for phone in preflight["phones"]:
            if phone["revoked_at"] is not None:
                action = "already revoked"
            elif phone["id"] in self.keep_phones:
                action = "keep"
            else:
                action = "revoke"
            kind = "phone" if phone["activated_at"] is not None else "code"
            self.output(f"  {phone['id']}: {phone['label']} ({kind}, {phone['source']}, active until {phone['active_until']}) -> {action}")
        if rejected:
            self.output(f"Photo keys with an unexpected format, never deleted: {len(rejected)}")
        self._s3_report(keys)
        self.output("Run order:")
        for step in RUN_ORDER:
            self.output("  " + step)

    def dry_run(self):
        connection, tx = self._connect()
        try:
            preflight = self._preflight(tx)
            state = self._state(tx)
            keys, rejected = self._photo_keys(tx)
            connection.rollback()
        finally:
            connection.close()
        self._print_state(preflight, state, keys, rejected)
        self.output("Dry run only: nothing was changed.")
        return {"state": state, "keys": keys}

    def _load_saved(self, output_dir):
        path = output_dir / STATE_FILE
        if not path.is_file():
            return None
        saved = json.loads(path.read_text())
        if saved.get("host") != self.expect_host or saved.get("database") != self.expect_db or saved.get("completed"):
            return None
        return saved

    def run(self, confirm, snapshot_id, typed_confirmation, output_dir, include_s3=False):
        if not confirm:
            raise DomainError("RESET_CONFIRM_REQUIRED")
        if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
            raise DomainError("RESET_SNAPSHOT_ID_REQUIRED")
        if typed_confirmation != confirmation_text(self.expect_db, self.expect_host):
            raise DomainError("RESET_CONFIRMATION_MISMATCH")
        if include_s3 and (self.s3 is None or not self.bucket):
            raise DomainError("RESET_S3_BUCKET_REQUIRED")
        output_dir = Path(output_dir)
        if not output_dir.is_dir():
            raise DomainError("RESET_OUTPUT_DIRECTORY_REQUIRED")
        self.output("RDS snapshot: " + snapshot_id)
        connection, tx = self._connect()
        try:
            preflight = self._preflight(tx)
            before = self._state(tx)
            keys, rejected = self._photo_keys(tx)
            connection.rollback()
            saved = self._load_saved(output_dir)
            counters = dict(before["counters"])
            if saved is not None:
                for name, value in saved["counters"].items():
                    counters[name] = max(counters.get(name) or 1, value)
                keys = sorted(set(keys) | set(saved["keys"]))
            state_path = output_dir / STATE_FILE
            state_path.write_text(json.dumps({
                "host": self.expect_host, "database": self.expect_db, "snapshot_id": snapshot_id,
                "counters": counters, "keys": keys, "counts": before["counts"], "completed": False,
            }, default=str, indent=2))
            state_path.chmod(0o600)
            self._print_state(preflight, before, keys, rejected)
            tx.execute("START TRANSACTION")
            now = tx.now()
            revoked = [row["id"] for row in tx.all(
                "SELECT id FROM scanner_phones WHERE revoked_at IS NULL ORDER BY id FOR UPDATE"
            ) if row["id"] not in self.keep_phones]
            for identifier in revoked:
                tx.execute(
                    "UPDATE scanner_phones SET revoked_at = %s, revoked_by_staff_id = %s WHERE id = %s AND revoked_at IS NULL",
                    (now, preflight["operator_id"], identifier),
                )
            connection.commit()
            tx.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
            try:
                for name in WIPE_TABLES:
                    if self.truncate_hook is not None:
                        self.truncate_hook(name)
                    tx.execute("TRUNCATE TABLE " + _quote_identifier(name))
                for name in WIPE_TABLES:
                    value = counters.get(name)
                    if name not in FRESH_COUNTER_TABLES and value and value > 1:
                        tx.execute("ALTER TABLE " + _quote_identifier(name) + " AUTO_INCREMENT = " + str(int(value)))
            finally:
                tx.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")
            tx.execute("START TRANSACTION")
            event_id = audit(tx, preflight["operator_id"], "TEST_DATA_RESET", "database", self.expect_db, after={
                "snapshot_id": snapshot_id, "operator_email": self.operator_email,
                "rows_removed": {name: before["counts"][name] for name in WIPE_TABLES},
                "phones_revoked": revoked, "phones_kept": list(self.keep_phones), "s3_requested": include_s3,
            })
            connection.commit()
            after = self._state(tx)
            self._verify(tx, before, after, event_id)
            connection.rollback()
        finally:
            connection.close()
        deleted = self._delete_s3(keys) if include_s3 else 0
        saved_state = json.loads(state_path.read_text())
        saved_state["completed"] = True
        saved_state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state_path.write_text(json.dumps(saved_state, default=str, indent=2))
        self.output("Rows after reset:")
        for name in sorted(after["counts"]):
            self.output(f"  {name}: {after['counts'][name]}")
        self.output("Triggers, CHECK constraints, foreign keys and the schema fingerprint are identical to before.")
        self.output(f"Kept tables are unchanged. Scanner phones revoked: {len(revoked)}; kept: {len(self.keep_phones)}.")
        self.output(f"Audit log restarted with TEST_DATA_RESET as entry {event_id}.")
        if include_s3:
            self.output(f"S3 objects deleted: {deleted}.")
        else:
            self.output(f"S3 not changed. {len(keys)} photo keys are saved in {state_path}.")
        self.output("Next: docker compose --env-file .env.docker up -d, then check both /health/ready endpoints.")
        return {"before": before, "after": after, "revoked": revoked, "event_id": event_id, "deleted": deleted}

    def _verify(self, tx, before, after, event_id):
        if after["guards"] != before["guards"]:
            changed = [name for name in before["guards"] if before["guards"][name] != after["guards"][name]]
            raise DomainError("RESET_GUARDS_CHANGED:" + ",".join(changed))
        if after["checksums"] != before["checksums"]:
            changed = [name for name in KEEP_TABLES if before["checksums"][name] != after["checksums"][name]]
            raise DomainError("RESET_KEPT_TABLES_CHANGED:" + ",".join(changed))
        remaining = [name for name in WIPE_TABLES if after["counts"][name] != (1 if name == "audit_events" else 0)]
        if remaining or event_id != 1:
            raise DomainError("RESET_INCOMPLETE:" + ",".join(remaining or ["audit_events"]))
        if after["counts"][PHONE_TABLE] != before["counts"][PHONE_TABLE]:
            raise DomainError("RESET_SCANNER_PHONES_CHANGED")
        if tx.one("SELECT @@SESSION.foreign_key_checks AS checks")["checks"] != 1:
            raise DomainError("RESET_FOREIGN_KEY_CHECKS_OFF")
        admins = tx.one(
            "SELECT COUNT(*) AS count FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN'"
        )["count"]
        if admins < 1:
            raise DomainError("RESET_NO_ACTIVE_ADMIN")

    def _delete_s3(self, keys):
        deleted = 0
        for key in keys:
            if not PHOTO_KEY.fullmatch(key):
                continue
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            deleted += 1
        return deleted
