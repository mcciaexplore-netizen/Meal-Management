import hashlib
import hmac
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .backup import _read_file, load_verified_backup, normalize_sql_mode, snapshot_database, table_digest
from .config import Settings
from .database import Database, Session
from .errors import DomainError
from .migrations import MigrationRunner, load_migrations, validate_ledger
from .runtime import RuntimeSettings
from .security import TokenVault, token_digest


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise DomainError("TRANSFER_INVALID_IDENTIFIER")
    return "`" + value + "`"


def _validate_target(settings, runtime, expected_target):
    target = settings.db_host + ":" + str(settings.db_port) + "/" + settings.db_name
    if expected_target != target:
        raise DomainError("TRANSFER_TARGET_CONFIRMATION_MISMATCH")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.aivencloud\.com", settings.db_host):
        raise DomainError("TRANSFER_REQUIRES_AIVEN_HOSTNAME")
    if not 1 <= settings.db_port <= 65535 or settings.db_user.lower() == "root":
        raise DomainError("TRANSFER_INVALID_AIVEN_CONFIGURATION")
    _identifier(settings.db_name)
    if not settings.db_ssl_ca:
        raise DomainError("TRANSFER_VERIFIED_TLS_REQUIRED")
    certificate = Path(settings.db_ssl_ca)
    if not certificate.is_absolute() or any(item.is_symlink() for item in (certificate, *certificate.parents)):
        raise DomainError("TRANSFER_VERIFIED_TLS_REQUIRED")
    try:
        if not stat.S_ISREG(certificate.stat().st_mode):
            raise DomainError("TRANSFER_VERIFIED_TLS_REQUIRED")
        ssl.create_default_context(cafile=str(certificate))
    except (OSError, ssl.SSLError):
        raise DomainError("TRANSFER_VERIFIED_TLS_REQUIRED") from None
    if (
        runtime.environment != "development"
        or runtime.photo_backend != "local"
        or runtime.email_backend != "preview"
        or runtime.email_send_enabled is not False
        or runtime.email_auto_send_enabled is not False
    ):
        raise DomainError("TRANSFER_REQUIRES_LOCAL_DEVELOPMENT_WITH_EMAIL_DISABLED")
    for application in ("admin", "scanner"):
        selected = runtime.for_application(application)
        origin = urlsplit(selected.app_origin)
        if origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1"}:
            raise DomainError("TRANSFER_REQUIRES_LOOPBACK_ORIGINS")
    return target


def _validate_backup_environment(settings, runtime, path, manifest):
    import io

    from dotenv import dotenv_values

    with _read_file(path / "environment.env") as stream:
        environment = dotenv_values(stream=io.StringIO(stream.read().decode("utf-8")), interpolate=False)
    original = Settings.from_env(environment)
    expected = ",".join(original.qr_encryption_keys).encode()
    actual = ",".join(settings.qr_encryption_keys).encode()
    if not hmac.compare_digest(expected, actual):
        raise DomainError("TRANSFER_QR_KEYS_MUST_MATCH_BACKUP")
    original_runtime = RuntimeSettings.from_env(environment)
    if not hmac.compare_digest(original_runtime.csrf_secret, runtime.csrf_secret) or not hmac.compare_digest(original_runtime.login_rate_secret, runtime.login_rate_secret):
        raise DomainError("TRANSFER_APPLICATION_SECRETS_MUST_MATCH_BACKUP")
    for name in (
        "app_origin", "scanner_origin", "allowed_hosts", "cookie_secure", "session_cookie",
        "scanner_enabled", "scanner_request_limit", "scanner_ip_limit", "scanner_window_seconds",
    ):
        if getattr(original_runtime, name) != getattr(runtime, name):
            raise DomainError("TRANSFER_BROWSER_CONFIGURATION_MUST_MATCH_BACKUP")
    if str(runtime.photo_root.absolute()) != manifest["photo_root"]:
        raise DomainError("TRANSFER_PHOTO_ROOT_MUST_MATCH_BACKUP")
    photo_root = runtime.photo_root.absolute()
    for name, checksum in manifest["files"].items():
        if not name.startswith("photos/"):
            continue
        photo = photo_root / name.removeprefix("photos/")
        if any(item.is_symlink() for item in (photo, *photo.parents)):
            raise DomainError("TRANSFER_LOCAL_PHOTOS_MUST_MATCH_BACKUP")
        try:
            descriptor = os.open(photo, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise DomainError("TRANSFER_LOCAL_PHOTOS_MUST_MATCH_BACKUP")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            raise DomainError("TRANSFER_LOCAL_PHOTOS_MUST_MATCH_BACKUP") from None
        if not hmac.compare_digest(digest, checksum):
            raise DomainError("TRANSFER_LOCAL_PHOTOS_MUST_MATCH_BACKUP")


def _backup_plan(manifest, directory):
    try:
        snapshot = manifest["snapshot"]
        ledger = snapshot["migrations"]
        pending = validate_ledger(load_migrations(directory), ledger)
        versions = [row["version"] for row in ledger]
        if versions not in ([1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6]):
            raise DomainError("TRANSFER_BACKUP_REQUIRES_MIGRATION_FIVE_OR_SIX")
        if [item.version for item in pending] not in ([], [6]):
            raise DomainError("TRANSFER_ONLY_MIGRATION_SIX_IS_ALLOWED")
        counts = snapshot["table_counts"]
        digests = snapshot["table_sha256"]
        columns = snapshot["table_columns"]
        if not {"schema_migrations", "qr_credentials", "email_queue"}.issubset(counts):
            raise ValueError
        if set(counts) != set(digests) or set(counts) != set(columns):
            raise ValueError
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in digests.values()):
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot["schema_fingerprint"]):
            raise ValueError
        for table, names in columns.items():
            _identifier(table)
            if not isinstance(names, list) or not names or len(set(names)) != len(names):
                raise ValueError
            for name in names:
                _identifier(name)
        if not isinstance(snapshot["triggers"], list):
            raise ValueError
        _validate_triggers(snapshot["triggers"])
        return snapshot, tuple(item.version for item in pending)
    except (KeyError, TypeError, ValueError):
        raise DomainError("TRANSFER_INVALID_BACKUP_MANIFEST") from None


def _require_empty(tx):
    for catalog, column in (
        ("TABLES", "TABLE_SCHEMA"), ("TRIGGERS", "TRIGGER_SCHEMA"),
        ("ROUTINES", "ROUTINE_SCHEMA"), ("EVENTS", "EVENT_SCHEMA"),
    ):
        row = tx.one("SELECT COUNT(*) AS count FROM information_schema." + catalog + " WHERE " + column + " = DATABASE()")
        if not row or row["count"] != 0:
            raise DomainError("TRANSFER_DESTINATION_MUST_BE_EMPTY")


def _option(value):
    value = str(value)
    if "\x00" in value:
        raise DomainError("TRANSFER_INVALID_CLIENT_OPTION")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def _import_portable(settings, backup_path, mysql):
    executable = shutil.which(mysql)
    if executable is None:
        raise DomainError("TRANSFER_MYSQL_CLIENT_REQUIRED")
    parent = backup_path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise DomainError("TRANSFER_BACKUP_PARENT_REQUIRES_PRIVATE_PERMISSIONS")
    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "protocol": "TCP",
        "ssl-mode": "VERIFY_IDENTITY",
        "ssl-ca": settings.db_ssl_ca,
        "default-character-set": "utf8mb4",
        "connect-timeout": settings.db_connect_timeout,
    }
    with tempfile.TemporaryDirectory(prefix="meal-restore-", dir=backup_path.parent) as temporary:
        option_path = Path(temporary) / "client.cnf"
        descriptor = os.open(option_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("[client]\n" + "".join(key + "=" + _option(value) + "\n" for key, value in options.items()))
        environment = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}
        with _read_file(backup_path / "portable.sql") as source:
            result = subprocess.run(
                [executable, "--defaults-file=" + str(option_path), "--no-login-paths", "--binary-mode",
                 "--batch", "--skip-reconnect", "--local-infile=0", "--database=" + settings.db_name],
                stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, check=False, timeout=3600,
            )
        if result.returncode != 0:
            raise DomainError("TRANSFER_IMPORT_FAILED_REQUIRES_INSPECTION")


def _trigger_semantics(triggers):
    return sorted(
        ({**{key: value for key, value in trigger.items() if key != "definer"},
          "sql_mode": normalize_sql_mode(trigger["sql_mode"])} for trigger in triggers),
        key=lambda trigger: trigger["name"],
    )


def _validate_triggers(triggers):
    seen = set()
    orders = {}
    try:
        for trigger in triggers:
            _identifier(trigger["name"])
            _identifier(trigger["table_name"])
            if trigger["name"] in seen:
                raise ValueError
            seen.add(trigger["name"])
            if trigger["timing"] not in {"BEFORE", "AFTER"} or trigger["event"] not in {"INSERT", "UPDATE", "DELETE"}:
                raise ValueError
            for key in ("character_set_client", "collation_connection", "database_collation"):
                _identifier(trigger[key])
            if not isinstance(trigger["body"], str) or not trigger["body"].strip():
                raise ValueError
            if not isinstance(trigger["sql_mode"], str) or type(trigger["action_order"]) is not int or trigger["action_order"] < 1:
                raise ValueError
            group = (trigger["table_name"], trigger["timing"], trigger["event"])
            orders.setdefault(group, []).append(trigger["action_order"])
        if any(sorted(order) != list(range(1, len(order) + 1)) for order in orders.values()):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("TRANSFER_INVALID_TRIGGER_METADATA") from None


def _restore_triggers(connection, tx, triggers, owner):
    if not isinstance(owner, str) or "@" not in owner or owner.split("@", 1)[0].lower() == "root":
        raise DomainError("TRANSFER_AIVEN_TRIGGER_OWNER_REQUIRED")
    _validate_triggers(triggers)
    ordered = sorted(triggers, key=lambda item: (item["table_name"], item["timing"], item["event"], item["action_order"], item["name"]))
    for trigger in ordered:
        name = _identifier(trigger["name"])
        table = _identifier(trigger["table_name"])
        if trigger["timing"] not in {"BEFORE", "AFTER"} or trigger["event"] not in {"INSERT", "UPDATE", "DELETE"}:
            raise DomainError("TRANSFER_INVALID_TRIGGER_METADATA")
        for key in ("character_set_client", "collation_connection", "database_collation"):
            _identifier(trigger[key])
        if not isinstance(trigger["body"], str) or not trigger["body"].strip() or type(trigger["action_order"]) is not int:
            raise DomainError("TRANSFER_INVALID_TRIGGER_METADATA")
        connection.set_charset_collation(trigger["character_set_client"], trigger["collation_connection"])
        tx.execute("SET SESSION sql_mode = %s", (trigger["sql_mode"],))
        tx.execute("CREATE TRIGGER " + name + " " + trigger["timing"] + " " + trigger["event"]
                   + " ON " + table + " FOR EACH ROW " + trigger["body"])
    connection.commit()


def _compare_restore(original, restored, owner):
    for key in ("schema_fingerprint", "migrations", "table_counts", "table_sha256", "table_columns"):
        if original[key] != restored[key]:
            raise DomainError("TRANSFER_RESTORED_SNAPSHOT_MISMATCH")
    if _trigger_semantics(original["triggers"]) != _trigger_semantics(restored["triggers"]):
        raise DomainError("TRANSFER_RESTORED_TRIGGERS_MISMATCH")
    if any(trigger["definer"] != owner for trigger in restored["triggers"]):
        raise DomainError("TRANSFER_RESTORED_TRIGGER_OWNER_MISMATCH")


def _email_before_migration(database, snapshot):
    columns = [column for column in snapshot["table_columns"]["email_queue"] if column not in {"status", "updated_at"}]
    with database.transaction() as tx:
        states = tx.all("SELECT id, status, updated_at FROM email_queue ORDER BY id")
        digest = table_digest(tx.connection, "email_queue", columns=columns)
    return columns, states, digest


def _verify_migration(database, original, upgraded, before, directory, owner):
    if validate_ledger(load_migrations(directory), upgraded["migrations"]):
        raise DomainError("TRANSFER_MIGRATION_VERIFICATION_FAILED")
    if set(upgraded["table_counts"]) != set(original["table_counts"]) | {"employee_email_batches"}:
        raise DomainError("TRANSFER_MIGRATION_VERIFICATION_FAILED")
    for table in original["table_counts"]:
        if table in {"email_queue", "schema_migrations"}:
            continue
        for key in ("table_counts", "table_sha256", "table_columns"):
            if original[key][table] != upgraded[key][table]:
                raise DomainError("TRANSFER_MIGRATION_CHANGED_UNRELATED_DATA")
    if upgraded["table_counts"]["employee_email_batches"] != 0:
        raise DomainError("TRANSFER_MIGRATION_VERIFICATION_FAILED")
    old_names = {trigger["name"] for trigger in original["triggers"]}
    new_names = {trigger["name"] for trigger in upgraded["triggers"]}
    if new_names != old_names | {"employee_email_batch_update_guard", "employee_email_batch_delete_guard", "email_queue_approval_guard"}:
        raise DomainError("TRANSFER_MIGRATION_VERIFICATION_FAILED")
    if _trigger_semantics(original["triggers"]) != _trigger_semantics([trigger for trigger in upgraded["triggers"] if trigger["name"] in old_names]):
        raise DomainError("TRANSFER_MIGRATION_CHANGED_EXISTING_TRIGGERS")
    if any(trigger["definer"] != owner for trigger in upgraded["triggers"]):
        raise DomainError("TRANSFER_RESTORED_TRIGGER_OWNER_MISMATCH")
    columns, original_states, original_digest = before
    with database.transaction() as tx:
        after_digest = table_digest(tx.connection, "email_queue", columns=columns)
        after_states = tx.all("SELECT id, status, updated_at FROM email_queue ORDER BY id")
        invalid = tx.one(
            "SELECT COUNT(*) AS count FROM email_queue WHERE delivery_mode <> 'LEGACY' "
            "OR bulk_batch_id IS NOT NULL OR approved_by_staff_id IS NOT NULL OR approved_at IS NOT NULL"
        )
        window = tx.one("SELECT started_at, applied_at FROM schema_migrations WHERE version = 6 AND status = 'APPLIED'")
    if after_digest != original_digest or len(after_states) != len(original_states) or invalid["count"] != 0:
        raise DomainError("TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED")
    bounds = (window.get("started_at"), window.get("applied_at")) if isinstance(window, dict) else (None, None)
    if any(not isinstance(value, datetime) or value.tzinfo is not None for value in bounds) or bounds[0] > bounds[1]:
        raise DomainError("TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED")
    for original_state, after_state in zip(original_states, after_states):
        expected_status = "PENDING_APPROVAL" if original_state["status"] == "QUEUED" else original_state["status"]
        original_updated = original_state.get("updated_at")
        after_updated = after_state.get("updated_at")
        if (
            after_state["id"] != original_state["id"] or after_state["status"] != expected_status
            or not isinstance(original_updated, datetime) or original_updated.tzinfo is not None
            or not isinstance(after_updated, datetime) or after_updated.tzinfo is not None
        ):
            raise DomainError("TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED")
        if original_state["status"] == "QUEUED":
            valid_timestamp = original_updated <= after_updated and bounds[0] <= after_updated <= bounds[1]
        else:
            valid_timestamp = after_updated == original_updated
        if not valid_timestamp:
            raise DomainError("TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED")


def _verify_qr_decryption(database, settings):
    vault = TokenVault(settings.qr_encryption_keys)
    verified = 0
    with database.transaction() as tx:
        rows = tx.all("SELECT token_hash, token_ciphertext FROM qr_credentials WHERE token_ciphertext IS NOT NULL ORDER BY id")
        for row in rows:
            token = vault.decrypt(row["token_ciphertext"])
            if not hmac.compare_digest(token_digest(token), bytes(row["token_hash"])):
                raise DomainError("TRANSFER_QR_DECRYPTION_VERIFICATION_FAILED")
            verified += 1
        for row in tx.all("SELECT payload_ciphertext FROM email_queue WHERE payload_ciphertext IS NOT NULL ORDER BY id"):
            json.loads(vault.decrypt(row["payload_ciphertext"]))
    return verified


def _write_result(path, result):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report = path.parent / (path.name + ".restore-" + timestamp + ".json")
    descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return str(report)


def restore_aiven_backup(settings, runtime, backup_path, migration_directory, *, expected_target,
                         allow_database_changes, writers_paused, mysql="mysql"):
    if allow_database_changes is not True:
        raise DomainError("TRANSFER_DATABASE_CHANGES_REQUIRE_APPROVAL")
    if writers_paused is not True:
        raise DomainError("TRANSFER_WRITERS_MUST_BE_PAUSED")
    target = _validate_target(settings, runtime, expected_target)
    path = Path(backup_path).absolute()
    manifest = load_verified_backup(path)
    original, pending = _backup_plan(manifest, migration_directory)
    _validate_backup_environment(settings, runtime, path, manifest)
    with _read_file(path / "triggers.json") as stream:
        triggers = json.load(stream)
    if triggers != original["triggers"]:
        raise DomainError("TRANSFER_BACKUP_TRIGGER_METADATA_MISMATCH")
    if shutil.which(mysql) is None:
        raise DomainError("TRANSFER_MYSQL_CLIENT_REQUIRED")
    database = Database(settings)
    connection = None
    tx = None
    locks = []
    started = False
    try:
        connection = database._connect()
        tx = Session(connection)
        server = tx.one("SELECT VERSION() AS version, DATABASE() AS name, CURRENT_USER() AS owner")
        if not server or not server["version"].startswith("8.4.") or server["name"] != settings.db_name:
            raise DomainError("TRANSFER_MYSQL_8_4_TARGET_REQUIRED")
        owner = server["owner"]
        if not isinstance(owner, str) or "@" not in owner or owner.split("@", 1)[0].lower() == "root":
            raise DomainError("TRANSFER_AIVEN_TRIGGER_OWNER_REQUIRED")
        suffix = hashlib.sha256(settings.db_name.encode()).hexdigest()[:40]
        for prefix in ("meal-transfer-", "meal-migrations-"):
            lock = prefix + suffix
            acquired = tx.one("SELECT GET_LOCK(%s, 0) AS acquired", (lock,))
            if not acquired or acquired["acquired"] != 1:
                raise DomainError("TRANSFER_DATABASE_OPERATION_ALREADY_RUNNING")
            locks.append(lock)
        _require_empty(tx)
        started = True
        _import_portable(settings, path, mysql)
        _restore_triggers(connection, tx, triggers, owner)
        restored = snapshot_database(database, migration_directory)
        _compare_restore(original, restored, owner)
        applied = ()
        if pending:
            before = _email_before_migration(database, original)
            migration_lock = locks.pop()
            tx.one("SELECT RELEASE_LOCK(%s) AS released", (migration_lock,))
            applied = MigrationRunner(database, migration_directory).apply()
            if applied != (6,):
                raise DomainError("TRANSFER_UNEXPECTED_MIGRATION_RESULT")
            restored = snapshot_database(database, migration_directory)
            _verify_migration(database, original, restored, before, migration_directory, owner)
        verified = _verify_qr_decryption(database, settings)
        with _read_file(path / "manifest.json") as stream:
            manifest_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        result = {
            "status": "verified", "target": target,
            "backup_manifest_sha256": manifest_sha256,
            "applied_migrations": list(applied), "table_counts": restored["table_counts"],
            "qr_credentials_verified": verified,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        result["report_file"] = _write_result(path, result)
        return result
    except DomainError:
        raise
    except Exception:
        code = "TRANSFER_FAILED_REQUIRES_INSPECTION" if started else "TRANSFER_PREFLIGHT_FAILED"
        raise DomainError(code) from None
    finally:
        if tx is not None:
            for lock in reversed(locks):
                try:
                    tx.one("SELECT RELEASE_LOCK(%s) AS released", (lock,))
                except Exception:
                    pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
