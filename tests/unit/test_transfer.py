import base64
import copy
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from meal_management.config import Settings
from meal_management.backup import normalize_sql_mode
from meal_management.errors import DomainError
from meal_management.migrations import load_migrations
from meal_management.runtime import RuntimeSettings
from meal_management.security import TokenVault, generate_token, token_digest
from meal_management.transfer import (
    _compare_restore, _email_before_migration, _import_portable, _restore_triggers, _verify_migration,
    _verify_qr_decryption, restore_aiven_backup,
)


DIRECTORY = Path(__file__).resolve().parents[2] / "database" / "migrations"


def ledger(last=5):
    return [{"version": item.version, "name": item.name, "checksum": item.checksum, "status": "APPLIED"} for item in load_migrations(DIRECTORY)[:last]]


def trigger(name="immutable_email", order=1):
    return {
        "name": name, "table_name": "email_queue", "timing": "BEFORE", "event": "UPDATE",
        "body": "BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Immutable'; END",
        "action_order": order, "sql_mode": "STRICT_TRANS_TABLES",
        "character_set_client": "utf8mb4", "collation_connection": "utf8mb4_0900_ai_ci",
        "database_collation": "utf8mb4_0900_ai_ci", "definer": "root@localhost",
    }


def snapshot(last=5):
    columns = {
        "schema_migrations": ["version", "name", "checksum", "status"],
        "qr_credentials": ["id", "token_hash", "token_ciphertext"],
        "email_queue": ["id", "status", "payload_ciphertext", "updated_at"],
    }
    result = {
        "version": "8.4.6", "database": "meal_management", "schema_fingerprint": "f" * 64,
        "migrations": ledger(last), "table_columns": columns,
        "table_counts": {"schema_migrations": last, "qr_credentials": 2, "email_queue": 3},
        "table_sha256": {name: "a" * 64 for name in columns}, "triggers": [trigger()], "photo_keys": [],
    }
    return result


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.backup = self.root / "backup"
        self.backup.mkdir(mode=0o700)
        (self.backup / "photos").mkdir(mode=0o700)
        self.certificate = self.root / "ca.pem"
        self.certificate.write_text("unit test certificate", encoding="utf-8")
        self.environment = {
            "DB_HOST": "localhost", "DB_PORT": "3306", "DB_NAME": "meal_management",
            "DB_USER": "local_runtime", "DB_PASSWORD": "local-private-password",
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"k" * 32).decode(),
            "APP_CSRF_SECRET": "c" * 32, "LOGIN_RATE_SECRET": "l" * 32,
            "APP_ENV": "development", "EMAIL_BACKEND": "preview", "EMAIL_SEND_ENABLED": "false",
            "EMAIL_AUTO_SEND_ENABLED": "false", "PRIVATE_PHOTO_ROOT": str(self.root / "photos"),
        }
        destination = dict(self.environment, DB_HOST="meal-project.a.aivencloud.com", DB_PORT="12345", DB_NAME="defaultdb",
                           DB_USER="avnadmin", DB_PASSWORD="aiven-private-password", DB_SSL_CA=str(self.certificate))
        self.settings = Settings.from_env(destination)
        self.runtime = RuntimeSettings.from_env(destination)
        self.target = "meal-project.a.aivencloud.com:12345/defaultdb"
        self.original = snapshot()
        self._write_backup()
        self.connection = Mock()
        self.database = Mock()
        self.database._connect.return_value = self.connection
        self.tx = Mock()
        self.tx.one.side_effect = self._query
        self.empty_catalog = None
        self.lock_available = True

    def _private_write(self, filename, content):
        path = self.backup / filename
        path.write_bytes(content)
        path.chmod(0o600)

    def _write_backup(self):
        self._private_write("original.sql", b"CREATE TABLE example (id INT);\n")
        self._private_write("portable.sql", b"CREATE TABLE example (id INT);\n")
        self._private_write("triggers.json", json.dumps(self.original["triggers"]).encode())
        self._private_write("environment.env", "\n".join(key + "=" + value for key, value in self.environment.items()).encode())
        files = {name: hashlib.sha256((self.backup / name).read_bytes()).hexdigest() for name in ("original.sql", "portable.sql", "triggers.json", "environment.env")}
        self.manifest = {"format_version": 1, "snapshot": self.original, "files": files,
                         "photo_root": str(self.root / "photos"), "created_at": "2026-09-10T00:00:00+00:00"}
        data = json.dumps(self.manifest).encode()
        self._private_write("manifest.json", data)
        self._private_write("COMPLETE", (hashlib.sha256(data).hexdigest() + "\n").encode())

    def _query(self, sql, params=()):
        if "CURRENT_USER()" in sql:
            return {"version": "8.4.9", "name": "defaultdb", "owner": "avnadmin@%"}
        if "GET_LOCK" in sql:
            return {"acquired": int(self.lock_available)}
        if "information_schema" in sql:
            return {"count": int(self.empty_catalog is not None and self.empty_catalog in sql)}
        if "RELEASE_LOCK" in sql:
            return {"released": 1}
        raise AssertionError("Unexpected query")

    def _call(self, **kwargs):
        arguments = {
            "expected_target": self.target, "allow_database_changes": True, "writers_paused": True,
        }
        arguments.update(kwargs)
        return restore_aiven_backup(self.settings, self.runtime, self.backup, DIRECTORY, **arguments)

    @contextmanager
    def _dependencies(self):
        with (
            patch("meal_management.transfer.ssl.create_default_context"),
            patch("meal_management.transfer.shutil.which", return_value="/approved/mysql"),
            patch("meal_management.transfer.Database", return_value=self.database) as database_factory,
            patch("meal_management.transfer.Session", return_value=self.tx),
            patch("meal_management.transfer._import_portable") as importer,
            patch("meal_management.transfer._restore_triggers") as triggers,
        ):
            yield database_factory, importer, triggers

    def test_change_and_pause_guards_precede_any_database_access(self):
        for kwargs, code in (
            ({"allow_database_changes": False}, "TRANSFER_DATABASE_CHANGES_REQUIRE_APPROVAL"),
            ({"writers_paused": False}, "TRANSFER_WRITERS_MUST_BE_PAUSED"),
        ):
            with self.subTest(code=code), self._dependencies() as (factory, importer, _):
                with self.assertRaisesRegex(DomainError, code):
                    self._call(**kwargs)
                factory.assert_not_called()
                importer.assert_not_called()

    def test_host_target_tls_and_runtime_guards_precede_database_access(self):
        original_settings, original_runtime = self.settings, self.runtime
        cases = [
            (replace(self.settings, db_ssl_ca=None), self.runtime, "TRANSFER_VERIFIED_TLS_REQUIRED"),
            (replace(self.settings, db_user="root"), self.runtime, "TRANSFER_INVALID_AIVEN_CONFIGURATION"),
            (self.settings, replace(self.runtime, email_send_enabled=True), "TRANSFER_REQUIRES_LOCAL_DEVELOPMENT"),
            (self.settings, replace(self.runtime, environment="production"), "TRANSFER_REQUIRES_LOCAL_DEVELOPMENT"),
        ]
        for settings, runtime, code in cases:
            with self.subTest(code=code), self._dependencies() as (factory, importer, _):
                self.settings, self.runtime = settings, runtime
                with self.assertRaisesRegex(DomainError, code):
                    self._call()
                factory.assert_not_called()
                importer.assert_not_called()
        self.settings, self.runtime = original_settings, original_runtime
        with self._dependencies() as (factory, _, _):
            with self.assertRaisesRegex(DomainError, "TRANSFER_TARGET_CONFIRMATION_MISMATCH"):
                self._call(expected_target="unconfirmed-target")
            self.settings = replace(self.settings, db_host="aivencloud.com.evil.example")
            with self.assertRaisesRegex(DomainError, "TRANSFER_REQUIRES_AIVEN_HOSTNAME"):
                self._call(expected_target=self.settings.db_host + ":12345/defaultdb")
            factory.assert_not_called()

    def test_mismatched_qr_and_browser_secrets_fail_before_connection(self):
        original_settings, original_runtime = self.settings, self.runtime
        for field in ("qr", "csrf", "login"):
            self.settings, self.runtime = original_settings, original_runtime
            if field == "qr":
                self.settings = replace(self.settings, qr_encryption_keys=(base64.urlsafe_b64encode(b"x" * 32).decode(),))
                code = "TRANSFER_QR_KEYS_MUST_MATCH_BACKUP"
            else:
                self.runtime = replace(self.runtime, **{"csrf_secret" if field == "csrf" else "login_rate_secret": b"x" * 32})
                code = "TRANSFER_APPLICATION_SECRETS_MUST_MATCH_BACKUP"
            with self.subTest(field=field), self._dependencies() as (factory, importer, _):
                with self.assertRaisesRegex(DomainError, code):
                    self._call()
                factory.assert_not_called()
                importer.assert_not_called()

    def test_checksum_failure_and_symlink_refuse_backup_before_connection(self):
        (self.backup / "portable.sql").write_bytes(b"altered contents")
        with self._dependencies() as (factory, importer, _):
            with self.assertRaisesRegex(DomainError, "BACKUP_FILE_CHECKSUM_MISMATCH"):
                self._call()
            factory.assert_not_called()
            importer.assert_not_called()
        self._write_backup()
        (self.backup / "portable.sql").unlink()
        (self.backup / "portable.sql").symlink_to(self.backup / "original.sql")
        with self._dependencies() as (factory, importer, _):
            with self.assertRaisesRegex(DomainError, "BACKUP_UNSAFE_PATH"):
                self._call()
            factory.assert_not_called()
            importer.assert_not_called()

    def test_every_schema_object_category_blocks_import(self):
        for catalog in ("TABLES", "TRIGGERS", "ROUTINES", "EVENTS"):
            self.empty_catalog = catalog
            with self.subTest(catalog=catalog), self._dependencies() as (_, importer, trigger_writer):
                with self.assertRaisesRegex(DomainError, "TRANSFER_DESTINATION_MUST_BE_EMPTY"):
                    self._call()
                importer.assert_not_called()
                trigger_writer.assert_not_called()
        self.connection.close.assert_called()

    def test_unavailable_lock_blocks_import(self):
        self.lock_available = False
        with self._dependencies() as (_, importer, _):
            with self.assertRaisesRegex(DomainError, "TRANSFER_DATABASE_OPERATION_ALREADY_RUNNING"):
                self._call()
            importer.assert_not_called()
            self.tx.execute.assert_not_called()

    def test_partial_import_failure_never_retries_or_drops_and_hides_driver_details(self):
        with self._dependencies() as (_, importer, trigger_writer):
            importer.side_effect = RuntimeError("aiven-private-password SELECT sensitive_payload")
            with self.assertRaises(DomainError) as caught:
                self._call()
            self.assertEqual(caught.exception.code, "TRANSFER_FAILED_REQUIRES_INSPECTION")
            self.assertNotIn("private-password", str(caught.exception))
            importer.assert_called_once()
            trigger_writer.assert_not_called()
            self.tx.execute.assert_not_called()
            self.connection.close.assert_called_once()
            releases = [call for call in self.tx.one.call_args_list if "RELEASE_LOCK" in call.args[0]]
            self.assertEqual(len(releases), 2)

    def test_schema_mismatch_stops_before_migration_or_qr_decryption(self):
        restored = copy.deepcopy(self.original)
        restored["table_sha256"]["email_queue"] = "b" * 64
        with self._dependencies(), patch("meal_management.transfer.snapshot_database", return_value=restored), patch("meal_management.transfer.MigrationRunner") as migrator, patch("meal_management.transfer._verify_qr_decryption") as decrypt:
            with self.assertRaisesRegex(DomainError, "TRANSFER_RESTORED_SNAPSHOT_MISMATCH"):
                self._call()
            migrator.assert_not_called()
            decrypt.assert_not_called()

    def test_verified_restore_records_only_safe_summary_outside_backup(self):
        self.original = snapshot(6)
        self._write_backup()
        restored = copy.deepcopy(self.original)
        restored["triggers"][0]["definer"] = "avnadmin@%"
        with self._dependencies(), patch("meal_management.transfer.snapshot_database", return_value=restored), patch("meal_management.transfer._verify_qr_decryption", return_value=2), patch("meal_management.transfer.MigrationRunner") as migrator:
            result = self._call()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["applied_migrations"], [])
        self.assertEqual(result["qr_credentials_verified"], 2)
        report = Path(result["report_file"])
        self.assertEqual(report.parent, self.backup.parent)
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
        self.assertNotIn(self.settings.db_password, report.read_text())
        self.assertNotIn(self.settings.qr_encryption_keys[0], report.read_text())
        migrator.assert_not_called()

    def test_verified_version_five_restore_applies_only_six_and_verifies_upgrade(self):
        restored = copy.deepcopy(self.original)
        restored["triggers"][0]["definer"] = "avnadmin@%"
        upgraded = copy.deepcopy(restored)
        upgraded["migrations"] = ledger(6)
        upgraded["table_counts"]["schema_migrations"] = 6
        upgraded["table_counts"]["employee_email_batches"] = 0
        before = object()
        with (
            self._dependencies(),
            patch("meal_management.transfer.snapshot_database", side_effect=[restored, upgraded]),
            patch("meal_management.transfer._email_before_migration", return_value=before),
            patch("meal_management.transfer._verify_migration") as verifier,
            patch("meal_management.transfer._verify_qr_decryption", return_value=2),
            patch("meal_management.transfer.MigrationRunner") as migrator,
        ):
            migrator.return_value.apply.return_value = (6,)
            result = self._call()
        self.assertEqual(result["applied_migrations"], [6])
        migrator.return_value.apply.assert_called_once_with()
        verifier.assert_called_once_with(self.database, self.original, upgraded, before, DIRECTORY, "avnadmin@%")

    def test_mysql_credentials_are_private_file_only_with_no_raw_output(self):
        option_files = []
        def execute(arguments, **kwargs):
            options = Path(arguments[1].removeprefix("--defaults-file="))
            option_files.append(options)
            self.assertEqual(stat.S_IMODE(options.stat().st_mode), 0o600)
            content = options.read_text()
            self.assertIn('ssl-mode="VERIFY_IDENTITY"', content)
            self.assertIn(self.settings.db_password, content)
            self.assertNotIn(self.settings.db_password, " ".join(arguments))
            self.assertIn("--no-login-paths", arguments)
            self.assertIn("--binary-mode", arguments)
            self.assertIn("--skip-reconnect", arguments)
            self.assertIn("--local-infile=0", arguments)
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(set(kwargs["env"]), {"PATH", "LC_ALL"})
            return Mock(returncode=1)
        with patch("meal_management.transfer.shutil.which", return_value="/approved/mysql"), patch("meal_management.transfer.subprocess.run", side_effect=execute) as run:
            with self.assertRaisesRegex(DomainError, "TRANSFER_IMPORT_FAILED_REQUIRES_INSPECTION"):
                _import_portable(self.settings, self.backup, "mysql")
        run.assert_called_once()
        self.assertTrue(option_files)
        self.assertTrue(all(not path.exists() for path in option_files))


class TriggerRestoreTests(unittest.TestCase):
    def test_restore_uses_sql_mode_string_and_ignores_equivalent_mode_order(self):
        original = snapshot()
        original["triggers"][0]["sql_mode"] = "STRICT_TRANS_TABLES,NO_ZERO_DATE"
        restored = copy.deepcopy(original)
        restored["triggers"][0]["definer"] = "avnadmin@%"
        restored["triggers"][0]["sql_mode"] = normalize_sql_mode({"NO_ZERO_DATE", "STRICT_TRANS_TABLES"})
        _compare_restore(original, restored, "avnadmin@%")
        connection, tx = Mock(), Mock()
        _restore_triggers(connection, tx, restored["triggers"], "avnadmin@%")
        modes = [call.args[1] for call in tx.execute.call_args_list if call.args[0] == "SET SESSION sql_mode = %s"]
        self.assertEqual(modes, [("NO_ZERO_DATE,STRICT_TRANS_TABLES",)])

    def test_triggers_use_current_account_without_importing_source_definer(self):
        connection, tx = Mock(), Mock()
        _restore_triggers(connection, tx, [trigger("second_guard", 2), trigger("first_guard", 1)], "avnadmin@%")
        created = [call.args[0] for call in tx.execute.call_args_list if call.args[0].startswith("CREATE TRIGGER")]
        self.assertEqual(len(created), 2)
        self.assertIn("`first_guard`", created[0])
        self.assertIn("`second_guard`", created[1])
        self.assertTrue(all("DEFINER" not in statement and "root@localhost" not in statement for statement in created))
        connection.set_charset_collation.assert_called_with("utf8mb4", "utf8mb4_0900_ai_ci")

    def test_root_owner_and_invalid_order_do_not_create_triggers(self):
        connection, tx = Mock(), Mock()
        for owner, triggers in (("root@localhost", [trigger()]), ("avnadmin@%", [trigger(order=2)])):
            with self.subTest(owner=owner), self.assertRaises(DomainError):
                _restore_triggers(connection, tx, triggers, owner)
        tx.execute.assert_not_called()

    def test_changed_trigger_body_or_definer_is_not_accepted(self):
        original = snapshot()
        for field, value in (("body", "BEGIN SELECT 1; END"), ("definer", "root@localhost")):
            restored = copy.deepcopy(original)
            restored["triggers"][0]["definer"] = "avnadmin@%"
            restored["triggers"][0][field] = value
            with self.subTest(field=field), self.assertRaises(DomainError):
                _compare_restore(original, restored, "avnadmin@%")


class MigrationAndDecryptionTests(unittest.TestCase):
    def setUp(self):
        self.original = snapshot()
        self.upgraded = copy.deepcopy(self.original)
        self.upgraded["migrations"] = ledger(6)
        self.upgraded["table_counts"]["employee_email_batches"] = 0
        self.upgraded["table_counts"]["schema_migrations"] = 6
        self.upgraded["triggers"].extend(trigger(name, order) for name, order in (
            ("employee_email_batch_update_guard", 2), ("employee_email_batch_delete_guard", 3), ("email_queue_approval_guard", 4),
        ))
        for item in self.upgraded["triggers"]:
            item["definer"] = "avnadmin@%"
        self.database, self.tx = Mock(), Mock()
        self.database.transaction.return_value.__enter__ = Mock(return_value=self.tx)
        self.database.transaction.return_value.__exit__ = Mock(return_value=False)
        self.old_updated = datetime(2026, 9, 9, 12, 0)
        self.started_at = datetime(2026, 9, 10, 12, 0)
        self.applied_at = self.started_at + timedelta(seconds=3)
        self.window = {"started_at": self.started_at, "applied_at": self.applied_at}
        self.states = [{"id": 1, "status": "QUEUED", "updated_at": self.old_updated},
                       {"id": 2, "status": "SENT", "updated_at": self.old_updated},
                       {"id": 3, "status": "FAILED", "updated_at": self.old_updated}]
        self.tx.all.return_value = [{"id": 1, "status": "PENDING_APPROVAL", "updated_at": self.started_at + timedelta(seconds=1)},
                                    {"id": 2, "status": "SENT", "updated_at": self.old_updated},
                                    {"id": 3, "status": "FAILED", "updated_at": self.old_updated}]
        self.tx.one.side_effect = lambda sql, params=(): self.window if "FROM schema_migrations" in sql else {"count": 0}
        self.before = (["id", "payload_ciphertext"], self.states, (3, "p" * 64))

    def _verify(self):
        _verify_migration(self.database, self.original, self.upgraded, self.before, DIRECTORY, "avnadmin@%")

    def test_migration_accepts_only_queue_hold_with_preserved_payloads(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            self._verify()
            self.tx.all.return_value[1]["status"] = "PENDING_APPROVAL"
            with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                self._verify()

    def test_baseline_separates_status_and_updated_at_from_preserved_fields(self):
        self.tx.all.return_value = self.states
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)) as digest:
            before = _email_before_migration(self.database, self.original)
        self.assertEqual(before, self.before)
        digest.assert_called_once_with(self.tx.connection, "email_queue", columns=["id", "payload_ciphertext"])
        self.tx.all.assert_called_once_with("SELECT id, status, updated_at FROM email_queue ORDER BY id")

    def test_migration_rejects_changed_terminal_updated_at(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            for index in (1, 2):
                with self.subTest(status=self.tx.all.return_value[index]["status"]):
                    self.tx.all.return_value[index]["updated_at"] = self.started_at
                    with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                        self._verify()
                    self.tx.all.return_value[index]["updated_at"] = self.old_updated

    def test_migration_rejects_queued_timestamp_outside_window_or_before_original(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            for timestamp in (self.started_at - timedelta(microseconds=1), self.applied_at + timedelta(microseconds=1), self.old_updated):
                with self.subTest(timestamp=timestamp):
                    self.tx.all.return_value[0]["updated_at"] = timestamp
                    with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                        self._verify()
            self.tx.all.return_value[0]["updated_at"] = self.started_at
            self.states[0]["updated_at"] = self.started_at + timedelta(seconds=1)
            with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                self._verify()

    def test_migration_accepts_queued_timestamp_at_window_boundaries(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            for timestamp in (self.started_at, self.applied_at):
                with self.subTest(timestamp=timestamp):
                    self.tx.all.return_value[0]["updated_at"] = timestamp
                    self._verify()

    def test_migration_rejects_invalid_ledger_timestamp_window(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            for window in (None, {}, {"started_at": self.started_at, "applied_at": None},
                           {"started_at": self.started_at.isoformat(), "applied_at": self.applied_at},
                           {"started_at": self.applied_at, "applied_at": self.started_at},
                           {"started_at": self.started_at.replace(tzinfo=timezone.utc), "applied_at": self.applied_at}):
                with self.subTest(window=window):
                    self.window = window
                    with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                        self._verify()

    def test_migration_rejects_missing_email_ids_or_invalid_row_timestamps(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "p" * 64)):
            self.tx.all.return_value[0]["id"] = 99
            with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                self._verify()
            self.tx.all.return_value[0]["id"] = 1
            self.tx.all.return_value[0]["updated_at"] = None
            with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                self._verify()

    def test_migration_rejects_changed_email_payloads_or_unrelated_rows(self):
        with patch("meal_management.transfer.table_digest", return_value=(3, "changed")):
            with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_EMAIL_PRESERVATION_FAILED"):
                self._verify()
        self.upgraded["table_sha256"]["qr_credentials"] = "b" * 64
        with self.assertRaisesRegex(DomainError, "TRANSFER_MIGRATION_CHANGED_UNRELATED_DATA"):
            self._verify()

    def test_decryption_checks_token_hash_and_encrypted_email_without_returning_secrets(self):
        key = base64.urlsafe_b64encode(b"k" * 32).decode()
        settings = Settings("localhost", 3306, "meal_management", "runtime", "private", (key,))
        vault = TokenVault((key,))
        token = generate_token()
        self.tx.all.side_effect = [
            [{"token_hash": token_digest(token), "token_ciphertext": vault.encrypt(token)}],
            [{"payload_ciphertext": vault.encrypt(json.dumps({"token": token}))}],
        ]
        self.assertEqual(_verify_qr_decryption(self.database, settings), 1)
        self.tx.all.side_effect = [[{"token_hash": b"x" * 32, "token_ciphertext": vault.encrypt(token)}]]
        with self.assertRaisesRegex(DomainError, "TRANSFER_QR_DECRYPTION_VERIFICATION_FAILED"):
            _verify_qr_decryption(self.database, settings)


if __name__ == "__main__":
    unittest.main()
