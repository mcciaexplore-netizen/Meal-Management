import base64
import copy
import getpass
import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from mysql.connector import Error as MySQLError, InternalError
from mysql.connector.constants import FieldFlag, FieldType
from mysql.connector.conversion import MySQLConverter

from meal_management.backup import (
    _backup_failure,
    _client_defaults,
    _dump,
    _dump_arguments,
    _expected_trigger_names,
    _json_bytes,
    canonical_value,
    backup_failure_message,
    create_local_backup,
    interactive_backup,
    load_verified_backup,
    normalize_sql_mode,
    snapshot_database,
    table_digest,
)
from meal_management.config import Settings
from meal_management.errors import DomainError
from meal_management.migrations import Migration


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = self.root / ".env"
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.photo = "a" * 64 + ".jpg"
        (self.photos / self.photo).write_bytes(b"private photo bytes")
        self.mapping = {
            "DB_HOST": "127.0.0.1", "DB_NAME": "meal_management", "DB_PORT": "3306",
            "DB_USER": "app", "DB_PASSWORD": "application-secret",
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"x" * 32).decode("ascii"),
        }
        self.env.write_text("".join(name + "=" + value + "\n" for name, value in self.mapping.items()))
        self.settings = Settings.from_env(self.mapping)
        self.snapshot = {
            "version": "8.4.11", "database": "meal_management", "schema_fingerprint": "a" * 64,
            "migrations": [{"version": 1, "name": "initial", "checksum": "b" * 64, "status": "APPLIED"}],
            "table_counts": {"employees": 1}, "table_sha256": {"employees": "c" * 64},
            "table_columns": {"employees": ["id", "selfie_object_key"]},
            "triggers": [{"name": "guard", "definer": "root@localhost"}], "photo_keys": [self.photo],
        }

    def run_process(self, arguments, **kwargs):
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, b"mysqldump Ver 8.4.11 for macos")
        self.assertNotIn("root-secret", " ".join(arguments))
        self.assertNotIn("MYSQL_PWD", kwargs["env"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)
        defaults = Path(arguments[1].split("=", 1)[1])
        self.assertEqual(stat.S_IMODE(defaults.stat().st_mode), 0o600)
        self.assertIn('password="root-secret"', defaults.read_text())
        kwargs["stdout"].write(b"CREATE TABLE employees (id INT PRIMARY KEY);\nINSERT INTO employees VALUES (1);\n")
        return subprocess.CompletedProcess(arguments, 0)

    def backup(self, snapshots=None, runner=None):
        with patch("meal_management.backup.shutil.which", return_value="/fake/mysqldump"):
            with patch("meal_management.backup.subprocess.run", side_effect=runner or self.run_process):
                with patch("meal_management.backup.snapshot_database", side_effect=snapshots or [self.snapshot, self.snapshot]):
                    return create_local_backup(
                        self.settings, project_root=self.root, env_file=self.env, photo_root=self.photos,
                        migration_directory=self.root, root_password="root-secret",
                    )

    def test_complete_backup_preserves_files_and_private_permissions(self):
        destination = self.backup()
        manifest = load_verified_backup(destination)
        self.assertEqual(manifest["snapshot"], self.snapshot)
        self.assertEqual((destination / "environment.env").read_bytes(), self.env.read_bytes())
        self.assertEqual((destination / "photos" / self.photo).read_bytes(), (self.photos / self.photo).read_bytes())
        self.assertEqual(set(manifest["files"]), {"original.sql", "portable.sql", "triggers.json", "environment.env", "photos/" + self.photo})
        for item in (destination, *destination.rglob("*")):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700 if item.is_dir() else 0o600)
            if item.is_file():
                self.assertNotIn(b"root-secret", item.read_bytes())
        self.assertFalse(list(destination.glob(".mysql-*")))

    def test_backup_uses_unique_directories_and_never_replaces_originals(self):
        first = self.backup()
        second = self.backup()
        self.assertNotEqual(first, second)
        self.assertEqual(self.env.read_text(), "".join(name + "=" + value + "\n" for name, value in self.mapping.items()))
        self.assertTrue((first / "COMPLETE").is_file())

    def test_invalid_metadata_stops_before_either_sql_export(self):
        for key, value in (("triggers", [{"sql_mode": {"STRICT_TRANS_TABLES"}}]), ("future_metadata", object())):
            with self.subTest(key=key), patch("meal_management.backup._dump") as dump:
                invalid = {**self.snapshot, key: value}
                with self.assertRaises(DomainError) as caught:
                    self.backup(snapshots=[invalid])
                self.assertEqual(caught.exception.backup_diagnostic["stage"], "SNAPSHOT_ENCODING")
                dump.assert_not_called()

    def test_source_changes_preserve_incomplete_backup_without_manifest_or_password(self):
        after = copy.deepcopy(self.snapshot)
        after["table_sha256"]["employees"] = "d" * 64
        with self.assertRaisesRegex(DomainError, "SOURCE_CHANGED"):
            self.backup([self.snapshot, after])
        destination = next((self.root / "var/private/backups").iterdir())
        self.assertFalse((destination / "COMPLETE").exists())
        self.assertFalse((destination / "manifest.json").exists())
        self.assertFalse(list(destination.glob(".mysql-*")))

    def test_failed_dump_never_marks_complete_and_does_not_expose_error(self):
        def failed(arguments, **kwargs):
            if "--version" in arguments:
                return self.run_process(arguments, **kwargs)
            raise RuntimeError("password=root-secret")

        with self.assertRaisesRegex(DomainError, "BACKUP_FAILED_PARTIAL_DIRECTORY_PRESERVED") as error:
            self.backup(runner=failed)
        self.assertNotIn("root-secret", str(error.exception))
        destination = next((self.root / "var/private/backups").iterdir())
        self.assertFalse((destination / "COMPLETE").exists())
        self.assertFalse(list(destination.glob(".mysql-*")))

    def test_login_failure_records_only_safe_stage_and_mysql_number(self):
        error = MySQLError(msg="credential=root-secret private employee content", errno=1045)
        with self.assertRaises(DomainError) as caught:
            self.backup(snapshots=[error])
        failure = caught.exception
        self.assertEqual(failure.backup_diagnostic, {
            "stage": "SOURCE_SNAPSHOT", "category": "MYSQL_ERROR", "mysql_error_number": 1045,
        })
        self.assertIn("MySQL rejected the login", backup_failure_message(failure))
        destination = next((self.root / "var/private/backups").iterdir())
        self.assertEqual({item.name for item in destination.iterdir()}, {"failure.json"})
        diagnostic = destination / "failure.json"
        self.assertEqual(stat.S_IMODE(diagnostic.stat().st_mode), 0o600)
        self.assertEqual(json.loads(diagnostic.read_bytes()), failure.backup_diagnostic)
        self.assertNotIn("root-secret", diagnostic.read_text())
        self.assertNotIn("private employee", diagnostic.read_text())
        self.assertNotIn("root-secret", backup_failure_message(failure))

    def test_export_failure_reports_export_phase_and_removes_credentials(self):
        def failed(arguments, **kwargs):
            if "--version" in arguments:
                return self.run_process(arguments, **kwargs)
            raise PermissionError("root-secret private path")

        with self.assertRaises(DomainError) as caught:
            self.backup(runner=failed)
        self.assertEqual(caught.exception.backup_diagnostic["stage"], "ORIGINAL_EXPORT")
        self.assertEqual(caught.exception.backup_diagnostic["category"], "FILE_PERMISSION_ERROR")
        destination = next((self.root / "var/private/backups").iterdir())
        self.assertFalse(list(destination.glob(".mysql-*")))
        self.assertFalse((destination / "COMPLETE").exists())
        self.assertNotIn("root-secret", (destination / "failure.json").read_text())

    def test_failure_report_write_error_does_not_hide_login_error(self):
        from meal_management.backup import _new_file

        def refuse_diagnostic(path):
            if path.name == "failure.json":
                raise PermissionError("root-secret")
            return _new_file(path)

        with patch("meal_management.backup._new_file", side_effect=refuse_diagnostic):
            with self.assertRaises(DomainError) as caught:
                self.backup(snapshots=[MySQLError(msg="root-secret", errno=1045)])
        self.assertEqual(caught.exception.backup_diagnostic["mysql_error_number"], 1045)
        self.assertNotIn("root-secret", str(caught.exception))

    def test_diagnostic_messages_never_include_raw_exception_text(self):
        for error in (
            MySQLError(msg="root-secret", errno=2003),
            MySQLError(msg="root-secret", errno=1064),
            ValueError("root-secret"),
            RuntimeError("root-secret"),
        ):
            with self.subTest(kind=type(error).__name__):
                failure = _backup_failure(error, "SOURCE_SNAPSHOT")
                self.assertIn("SOURCE_SNAPSHOT", backup_failure_message(failure))
                self.assertNotIn("root-secret", json.dumps(failure.backup_diagnostic))
                self.assertNotIn("root-secret", backup_failure_message(failure))

    def test_formatter_refuses_unknown_stage_or_category(self):
        for diagnostic in (
            None,
            {"stage": [], "category": "MYSQL_ERROR"},
            {"stage": "SOURCE_SNAPSHOT", "category": {}},
            {"stage": "private-token", "category": "MYSQL_ERROR"},
            {"stage": "SOURCE_SNAPSHOT", "category": "private-token"},
        ):
            failure = DomainError("BACKUP_FAILED_PARTIAL_DIRECTORY_PRESERVED")
            failure.backup_diagnostic = diagnostic
            self.assertIsNone(backup_failure_message(failure))

    def test_missing_referenced_photo_rejected_before_dump(self):
        (self.photos / self.photo).unlink()
        with self.assertRaisesRegex(DomainError, "REFERENCED_PHOTO_MISSING"):
            self.backup()
        destination = next((self.root / "var/private/backups").iterdir())
        self.assertFalse((destination / "original.sql").exists())

    def test_nonmatching_saved_environment_is_rejected_without_database_or_process(self):
        self.env.write_text(self.env.read_text().replace("application-secret", "other-secret"))
        with patch("meal_management.backup.Database") as database, patch("meal_management.backup.subprocess.run") as process:
            with self.assertRaisesRegex(DomainError, "ENVIRONMENT_DIFFERS"):
                self.backup()
            database.assert_not_called()
            process.assert_not_called()

    def test_recursive_photo_root_is_rejected(self):
        with self.assertRaisesRegex(DomainError, "PHOTO_ROOT_OVERLAPS"):
            create_local_backup(
                self.settings, project_root=self.root, env_file=self.env, photo_root=self.root / "var/private",
                migration_directory=self.root, root_password="root-secret",
            )

    def test_symlinked_photo_is_rejected(self):
        (self.photos / "link").symlink_to(self.env)
        with self.assertRaisesRegex(DomainError, "UNSUPPORTED_PHOTO_STORAGE"):
            self.backup()

    def test_tampered_file_is_rejected(self):
        destination = self.backup()
        (destination / "portable.sql").write_bytes(b"changed")
        with self.assertRaisesRegex(DomainError, "CHECKSUM_MISMATCH"):
            load_verified_backup(destination)

    def test_missing_completion_marker_is_rejected(self):
        destination = self.backup()
        (destination / "COMPLETE").unlink()
        with self.assertRaisesRegex(DomainError, "INCOMPLETE"):
            load_verified_backup(destination)

    def test_backup_with_readable_private_file_is_rejected(self):
        destination = self.backup()
        (destination / "environment.env").chmod(0o644)
        with self.assertRaisesRegex(DomainError, "PRIVATE_PERMISSIONS"):
            load_verified_backup(destination)

    def test_even_checksummed_extra_non_photo_file_is_rejected(self):
        destination = self.backup()
        extra = destination / "report.json"
        extra.write_bytes(b"{}")
        extra.chmod(0o600)
        manifest = json.loads((destination / "manifest.json").read_bytes())
        manifest["files"]["report.json"] = hashlib.sha256(extra.read_bytes()).hexdigest()
        data = json.dumps(manifest).encode()
        (destination / "manifest.json").write_bytes(data)
        (destination / "COMPLETE").write_text(hashlib.sha256(data).hexdigest() + "\n")
        with self.assertRaisesRegex(DomainError, "UNEXPECTED_FILE"):
            load_verified_backup(destination)

    def test_defaults_escape_password_and_are_removed_on_failure(self):
        settings = Settings.from_env({**self.mapping, "DB_PASSWORD": 'quote"\\\n\r\tend'})
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with _client_defaults(settings, self.root) as defaults:
                self.assertIn('password="quote\\"\\\\\\n\\r\\tend"', defaults.read_text())
                raise RuntimeError("stop")
        self.assertFalse(defaults.exists())

    @unittest.skipUnless(shutil.which("mysqldump"), "Offline MySQL client option check requires mysqldump on PATH")
    def test_installed_mysqldump_accepts_generated_defaults_and_both_export_commands(self):
        binary = shutil.which("mysqldump")
        with _client_defaults(self.settings, self.root) as defaults:
            for triggers in (True, False):
                with self.subTest(triggers=triggers):
                    arguments = _dump_arguments(binary, defaults, "fictional_database", triggers=triggers)
                    result = subprocess.run(
                        [*arguments[:-1], "--help"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        env={"PATH": str(Path(binary).parent), "LC_ALL": "C"},
                        check=False, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, "mysqldump rejected generated backup options")
        self.assertFalse(defaults.exists())

    def test_defaults_collision_preserves_the_preexisting_file(self):
        existing = self.root / ".mysql-fixed.cnf"
        existing.write_bytes(b"original")
        with patch("meal_management.backup.secrets.token_hex", return_value="fixed"):
            with self.assertRaises(FileExistsError):
                with _client_defaults(self.settings, self.root):
                    self.fail("Existing credentials must never be replaced")
        self.assertEqual(existing.read_bytes(), b"original")

    def test_portable_dump_options_avoid_locks_drop_and_credentials(self):
        with _client_defaults(self.settings, self.root) as defaults:
            def process(arguments, **kwargs):
                self.assertIn("--single-transaction", arguments)
                self.assertIn("--skip-lock-tables", arguments)
                self.assertIn("--skip-add-locks", arguments)
                self.assertIn("--skip-disable-keys", arguments)
                self.assertIn("--skip-add-drop-table", arguments)
                self.assertIn("--skip-add-drop-trigger", arguments)
                self.assertIn("--skip-triggers", arguments)
                self.assertIn("--hex-blob", arguments)
                self.assertIn("--column-statistics=0", arguments)
                self.assertNotIn("--databases", arguments)
                self.assertEqual(arguments[1], "--defaults-file=" + str(defaults))
                self.assertEqual(arguments[2], "--no-login-paths")
                self.assertEqual(arguments[-1], "meal_management")
                self.assertNotIn("application-secret", str(arguments))
                kwargs["stdout"].write(b"SELECT 1;\n")
                return subprocess.CompletedProcess(arguments, 0)

            with patch("meal_management.backup.subprocess.run", side_effect=process):
                _dump("/fake/mysqldump", defaults, "meal_management", self.root / "dump.sql", triggers=False)


class BackupDigestTests(unittest.TestCase):
    def test_sql_modes_normalize_connector_sets_empty_sets_and_string_order(self):
        description = ("sql_mode", FieldType.STRING, None, None, None, None, False, FieldFlag.SET, 45)
        converted = MySQLConverter().to_python(description, b"STRICT_TRANS_TABLES,NO_ZERO_DATE")
        self.assertIsInstance(converted, set)
        with self.assertRaises(TypeError):
            _json_bytes({"sql_mode": converted})
        for value in (converted, frozenset(converted), "STRICT_TRANS_TABLES,NO_ZERO_DATE", "NO_ZERO_DATE,STRICT_TRANS_TABLES"):
            with self.subTest(kind=type(value).__name__):
                normalized = normalize_sql_mode(value)
                self.assertEqual(normalized, "NO_ZERO_DATE,STRICT_TRANS_TABLES")
                self.assertEqual(json.loads(_json_bytes({"sql_mode": normalized}))["sql_mode"], normalized)
        for value in (set(), frozenset(), ""):
            self.assertEqual(normalize_sql_mode(value), "")

    def test_invalid_sql_mode_types_and_tokens_are_rejected(self):
        for value in (None, 1, b"STRICT_TRANS_TABLES", ["STRICT_TRANS_TABLES"], {1}, "STRICT_TRANS_TABLES,", "private text"):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(DomainError, "BACKUP_INVALID_TRIGGER_SQL_MODE"):
                normalize_sql_mode(value)

    def test_complete_snapshot_normalizes_real_connector_set_metadata_for_json(self):
        description = ("sql_mode", FieldType.STRING, None, None, None, None, False, FieldFlag.SET, 45)
        modes = MySQLConverter().to_python(description, b"STRICT_TRANS_TABLES,NO_ZERO_DATE")
        database = Mock()
        tx = Mock()
        tx.one.side_effect = [{"version": "8.4.11"}, {"name": "meal_management"}]
        ledger = [{"version": 1, "name": "initial", "checksum": "a", "status": "APPLIED"}]
        source_trigger = {
            "name": "guard", "table_name": "schema_migrations", "timing": "BEFORE", "event": "UPDATE",
            "body": "BEGIN SIGNAL SQLSTATE '45000'; END", "action_order": 1, "sql_mode": modes,
            "character_set_client": "utf8mb4", "collation_connection": "utf8mb4_0900_ai_ci",
            "database_collation": "utf8mb4_0900_ai_ci", "definer": "root@localhost",
        }
        tx.all.side_effect = [
            [{"name": "schema_migrations", "type": "BASE TABLE", "engine": "InnoDB"}],
            [], [], ledger, [source_trigger], [{"name": "version"}],
        ]
        migrations = (Migration(1, "initial", "a", ("CREATE TRIGGER guard BEFORE UPDATE ON schema_migrations FOR EACH ROW BEGIN END",)),)
        with (
            patch("meal_management.backup.load_migrations", return_value=migrations),
            patch("meal_management.backup.Session", return_value=tx),
            patch("meal_management.backup.table_digest", return_value=(1, "b" * 64)),
            patch("meal_management.backup.schema_fingerprint", return_value="c" * 64),
        ):
            result = snapshot_database(database, "unused")
        self.assertEqual(json.loads(_json_bytes(result)), result)
        self.assertEqual(result["triggers"][0]["sql_mode"], "NO_ZERO_DATE,STRICT_TRANS_TABLES")
        self.assertIs(source_trigger["sql_mode"], modes)
        self.assertEqual(result["triggers"][0]["body"], source_trigger["body"])
        trigger_query = next(call.args[0] for call in tx.all.call_args_list if "information_schema.TRIGGERS" in call.args[0])
        self.assertIn("CAST(SQL_MODE AS CHAR) AS sql_mode", trigger_query)

    def test_canonical_values_preserve_types_and_binary_data(self):
        values = [None, False, 0, "0", Decimal("0"), 0.0, b"0", datetime(2026, 1, 1), timedelta(microseconds=-1)]
        encoded = [json.dumps(canonical_value(value)) for value in values]
        self.assertEqual(len(encoded), len(set(encoded)))
        self.assertEqual(canonical_value(b"\x00\xff"), ["bytes", "AP8="])
        with self.assertRaisesRegex(DomainError, "UNSUPPORTED_COLUMN_VALUE"):
            canonical_value(float("nan"))

    def test_digest_streams_rows_in_primary_key_order_and_allows_source_columns(self):
        connection = Mock()
        tx = Mock()
        tx.all.side_effect = [[{"name": "id"}, {"name": "data"}, {"name": "new_column"}], [{"name": "id"}]]
        cursor = connection.cursor.return_value
        cursor.fetchmany.side_effect = [[(1, b"data"), (2, b"other")], []]
        with patch("meal_management.backup.Session", return_value=tx):
            count, checksum = table_digest(connection, "employees", columns=["id", "data"])
        self.assertEqual(count, 2)
        self.assertEqual(len(checksum), 64)
        connection.cursor.assert_called_once_with(buffered=False)
        cursor.execute.assert_called_once_with("SELECT `id`, `data` FROM `employees` ORDER BY `id`")
        cursor.close.assert_called_once()

    def test_digest_rejects_tables_without_primary_key(self):
        connection = Mock()
        tx = Mock()
        tx.all.side_effect = [[{"name": "id"}], []]
        with patch("meal_management.backup.Session", return_value=tx):
            with self.assertRaisesRegex(DomainError, "REQUIRES_PRIMARY_KEYS"):
                table_digest(connection, "employees")
        connection.cursor.assert_not_called()

    def test_unread_cursor_close_does_not_mask_row_processing_failure(self):
        connection = Mock()
        tx = Mock()
        tx.all.side_effect = [[{"name": "id"}], [{"name": "id"}]]
        cursor = connection.cursor.return_value
        cursor.fetchmany.return_value = [(object(),)]
        cursor.close.side_effect = InternalError("Unread result found")
        with patch("meal_management.backup.Session", return_value=tx):
            with self.assertRaisesRegex(DomainError, "BACKUP_UNSUPPORTED_COLUMN_VALUE"):
                table_digest(connection, "employees")
        cursor.close.assert_called_once()

    def test_snapshot_cleanup_preserves_original_database_error(self):
        database = Mock()
        connection = database._connect.return_value
        error = MySQLError(msg="private root-secret", errno=1064)
        connection.start_transaction.side_effect = error
        connection.rollback.side_effect = RuntimeError("cleanup failure")
        connection.close.side_effect = RuntimeError("close failure")
        with patch("meal_management.backup.load_migrations", return_value=()):
            with self.assertRaises(MySQLError) as caught:
                snapshot_database(database, "unused")
        self.assertIs(caught.exception, error)
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_expected_trigger_names_follow_applied_migration_changes(self):
        migrations = (
            Migration(1, "initial", "a", ("CREATE TRIGGER guard BEFORE DELETE ON things FOR EACH ROW BEGIN END",)),
            Migration(2, "next", "b", ("DROP TRIGGER IF EXISTS guard", "CREATE TRIGGER replacement BEFORE DELETE ON things FOR EACH ROW BEGIN END")),
            Migration(3, "future", "c", ("CREATE TRIGGER future BEFORE DELETE ON things FOR EACH ROW BEGIN END",)),
        )
        self.assertEqual(_expected_trigger_names(migrations, [{"version": 1}, {"version": 2}]), {"replacement"})

    def test_snapshot_rejects_invisible_expected_triggers_and_closes_connection(self):
        database = Mock()
        connection = database._connect.return_value
        tx = Mock()
        tx.one.side_effect = [{"version": "8.4.11"}, {"name": "meal_management"}]
        ledger = [{"version": 1, "name": "initial", "checksum": "a", "status": "APPLIED"}]
        tx.all.side_effect = [[{"name": "schema_migrations", "type": "BASE TABLE", "engine": "InnoDB"}], [], [], ledger, []]
        migrations = (Migration(1, "initial", "a", ("CREATE TRIGGER guard BEFORE DELETE ON things FOR EACH ROW BEGIN END",)),)
        with patch("meal_management.backup.load_migrations", return_value=migrations), patch("meal_management.backup.Session", return_value=tx):
            with self.assertRaisesRegex(DomainError, "TRIGGERS_MISSING_OR_NOT_VISIBLE"):
                snapshot_database(database, "unused")
        connection.start_transaction.assert_called_once_with(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_snapshot_rejects_views_nontransactional_tables_routines_and_events(self):
        cases = [
            ([[{"name": "things", "type": "VIEW", "engine": None}]], "INNODB_BASE_TABLES"),
            ([[{"name": "things", "type": "BASE TABLE", "engine": "MyISAM"}]], "INNODB_BASE_TABLES"),
            ([[{"name": "things", "type": "BASE TABLE", "engine": "InnoDB"}], [{"ROUTINE_NAME": "routine"}]], "UNSUPPORTED_ROUTINES"),
            ([[{"name": "things", "type": "BASE TABLE", "engine": "InnoDB"}], [], [{"EVENT_NAME": "event"}]], "UNSUPPORTED_EVENTS"),
        ]
        for results, code in cases:
            with self.subTest(code=code):
                database = Mock()
                tx = Mock()
                tx.one.side_effect = [{"version": "8.4.11"}, {"name": "meal_management"}]
                tx.all.side_effect = results
                with patch("meal_management.backup.load_migrations", return_value=()), patch("meal_management.backup.Session", return_value=tx):
                    with self.assertRaisesRegex(DomainError, code):
                        snapshot_database(database, "unused")
                database._connect.return_value.close.assert_called_once()


class InteractiveBackupTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("127.0.0.1", 3306, "meal_management", "app", "secret", ("key",))
        self.arguments = {"project_root": "project", "env_file": ".env", "photo_root": "photos", "migration_directory": "migrations"}

    def test_flags_and_tty_required_before_prompt_or_database_access(self):
        for flags, code in [({}, "ALLOW_DATABASE_ACCESS"), ({"allow_database_access": True}, "WRITERS_PAUSED"), ({"allow_database_access": True, "writers_paused": True}, "INTERACTIVE_TERMINAL")]:
            with self.subTest(flags=flags):
                with patch("meal_management.backup.sys.stdin.isatty", return_value=False), patch("meal_management.backup.getpass.getpass") as prompt, patch("meal_management.backup.create_local_backup") as create:
                    with self.assertRaisesRegex(DomainError, code):
                        interactive_backup(self.settings, **self.arguments, **flags)
                    prompt.assert_not_called()
                    create.assert_not_called()

    def test_wrong_exact_database_confirmation_does_not_prompt_password(self):
        with patch("meal_management.backup.sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"), patch("meal_management.backup.getpass.getpass") as prompt:
            with self.assertRaisesRegex(DomainError, "CONFIRMATION_MISMATCH"):
                interactive_backup(self.settings, **self.arguments, allow_database_access=True, writers_paused=True)
            prompt.assert_not_called()

    def test_secure_password_prompt_warning_is_fatal(self):
        with patch("meal_management.backup.sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="BACKUP 127.0.0.1:3306/meal_management"), patch("meal_management.backup.getpass.getpass", side_effect=getpass.GetPassWarning):
            with self.assertRaisesRegex(DomainError, "SECURE_PASSWORD_PROMPT_UNAVAILABLE"):
                interactive_backup(self.settings, **self.arguments, allow_database_access=True, writers_paused=True)

    def test_confirmed_backup_passes_ephemeral_password_only_to_library(self):
        with patch("meal_management.backup.sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="BACKUP 127.0.0.1:3306/meal_management"), patch("meal_management.backup.getpass.getpass", return_value="root-secret"), patch("meal_management.backup.create_local_backup", return_value=Path("backup")) as create:
            result = interactive_backup(self.settings, **self.arguments, allow_database_access=True, writers_paused=True)
        self.assertEqual(result, Path("backup"))
        self.assertEqual(create.call_args.kwargs["root_password"], "root-secret")


if __name__ == "__main__":
    unittest.main()
