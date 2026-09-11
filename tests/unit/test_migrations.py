import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from meal_management.cli import main
from meal_management.errors import DomainError
from meal_management.migrations import Migration, MigrationRunner, load_migrations, sql_statements, validate_ledger


class MigrationParserTests(unittest.TestCase):
    def test_parses_trigger_delimiters_without_splitting_trigger_body(self):
        source = (
            "SET time_zone = '+00:00';\n\nDELIMITER $$\n\n"
            "CREATE TRIGGER test BEFORE DELETE ON meals FOR EACH ROW\n"
            "BEGIN\nSIGNAL SQLSTATE '45000';\nEND$$\n\nDELIMITER ;\n"
        )
        statements = tuple(sql_statements(source))
        self.assertEqual(len(statements), 2)
        self.assertIn("SIGNAL SQLSTATE '45000';", statements[1])
        self.assertTrue(statements[1].endswith("END"))

    def test_rejects_unterminated_statement(self):
        with self.assertRaisesRegex(DomainError, "UNTERMINATED"):
            tuple(sql_statements("CREATE TABLE missing (id INT)"))

    def test_repository_migrations_have_stable_sequence(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migrations = load_migrations(directory)
        self.assertEqual([item.version for item in migrations], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertGreater(len(migrations[0].statements), 20)
        self.assertIn("scan_bound_at", "\n".join(migrations[1].statements))
        self.assertIn("last_seen_at", "\n".join(migrations[1].statements))

    def test_record_removal_is_append_only_and_matches_fresh_schema(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[6]
        source = "\n".join(migration.statements)
        snapshot = tuple(sql_statements((directory.parent / "schema.sql").read_text()))
        self.assertEqual(migration.name, "admin_record_removal")
        self.assertIn("CREATE TABLE employee_archives", source)
        self.assertIn("CREATE TABLE meal_voids", source)
        self.assertNotIn("DELETE FROM", source)
        for statement in migration.statements[1:]:
            self.assertIn(statement, snapshot)

    def test_employee_contact_migration_preserves_existing_employees(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[7]
        source = "\n".join(migration.statements)
        schema = (directory.parent / "schema.sql").read_text()
        self.assertEqual(migration.name, "employee_contact_details")
        self.assertIn("ADD COLUMN company_name VARCHAR(150) NULL", source)
        self.assertIn("ADD COLUMN phone VARCHAR(32) NULL", source)
        self.assertNotIn("UPDATE employees", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertIn("company_name VARCHAR(150) NULL", schema)
        self.assertIn("phone VARCHAR(32) NULL", schema)

    def test_master_allowance_migration_enforces_kind_usage_and_exhaustion(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[8]
        source = "\n".join(migration.statements)
        schema = (directory.parent / "schema.sql").read_text()
        self.assertEqual(migration.name, "master_qr_meal_allowances")
        self.assertIn("CREATE TABLE master_qr_allocations", source)
        self.assertIn("FOREIGN KEY (qr_id, kind) REFERENCES qr_credentials (id, kind)", source)
        self.assertIn("meal_limit > 0 AND meals_used <= meal_limit", source)
        self.assertIn("meals_used = meal_limit AND exhausted_at IS NOT NULL", source)
        self.assertNotIn("DELETE FROM", source)
        for statement in migration.statements[1:]:
            self.assertIn(statement, tuple(sql_statements(schema)))

    def test_rejects_migration_gap(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "002_later.sql").write_text("SELECT 1;\n")
            with self.assertRaisesRegex(DomainError, "CONTIGUOUS"):
                load_migrations(root)

    def test_direct_visitor_migration_preserves_existing_rows_and_tables(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[3]
        self.assertEqual(migration.version, 4)
        self.assertEqual(migration.name, "direct_master_visitor_meals")
        self.assertEqual(len(migration.statements), 6)
        for statement in migration.statements:
            self.assertFalse(statement.startswith(("DELETE ", "UPDATE ", "INSERT ", "DROP TABLE ", "TRUNCATE ")))
        source = "\n".join(migration.statements)
        self.assertIn("quantity > 0 AND authorization_id IS NOT NULL", source)
        self.assertIn("quantity = 1 AND authorization_id IS NULL", source)
        for column in ("visitor_company_name", "visitor_name", "visitor_email", "visitor_phone"):
            self.assertIn(f"ADD COLUMN {column} VARCHAR(", source)
            self.assertIn(f"AND CHAR_LENGTH(TRIM({column})) > 0", source)

    def test_direct_visitor_request_binding_requires_time_and_remains_immutable(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[3]
        request_alter = migration.statements[1]
        self.assertIn("ADD COLUMN scan_visitor_hash BINARY(32) NULL", request_alter)
        self.assertIn("scan_visitor_hash IS NULL OR scan_bound_at IS NOT NULL", request_alter)
        trigger = migration.statements[-1]
        self.assertIn("OLD.scan_bound_at IS NOT NULL OR OLD.status <> 'PENDING'", trigger)
        self.assertIn("NOT (NEW.scan_visitor_hash <=> OLD.scan_visitor_hash)", trigger)
        self.assertIn("NOT (NEW.scan_authorization_id <=> OLD.scan_authorization_id)", trigger)
        self.assertIn("NOT (NEW.scan_bound_at <=> OLD.scan_bound_at)", trigger)
        snapshot = tuple(sql_statements((directory.parent / "schema.sql").read_text()))
        self.assertIn(trigger, snapshot)

    def test_pending_master_read_is_a_finalized_attempt_without_a_meal(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        statement = load_migrations(directory)[3].statements[3]
        self.assertIn("ENUM('RECEIVED', 'SUCCESS', 'REJECTED', 'AWAITING_DETAILS')", statement)
        awaiting = statement.split("outcome = 'AWAITING_DETAILS'", 1)[1]
        self.assertIn("completed_at IS NOT NULL", awaiting)
        self.assertIn("request_id IS NOT NULL AND qr_id IS NOT NULL", awaiting)
        self.assertIn("staff_id IS NOT NULL AND scanner_id IS NOT NULL AND location_id IS NOT NULL", awaiting)
        self.assertIn("serving_id IS NULL AND rejection_code IS NULL", awaiting)

    def test_shared_scanner_migration_has_no_password_and_no_collision_overwrite(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[4]
        self.assertEqual(migration.name, "shared_meal_scanner")
        source = "\n".join(migration.statements)
        self.assertIn("(is_scanner = 0 AND password_hash IS NOT NULL AND CHAR_LENGTH(password_hash) > 0)", source)
        self.assertIn("OR (is_scanner = 1 AND password_hash IS NULL)", source)
        self.assertIn("VALUES ('Meal Scanner', 'meal-scanner@system.invalid', NULL, TRUE, TRUE)", source)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", source)
        self.assertNotIn("INSERT IGNORE", source)
        self.assertNotIn("UPDATE staff_accounts SET", source)
        self.assertTrue(any(statement.startswith("CREATE TRIGGER human_session_insert_guard") for statement in migration.statements))
        self.assertTrue(any(statement.startswith("CREATE TRIGGER scanner_role_insert_guard") for statement in migration.statements))

    def test_shared_scanner_schema_and_migration_preserve_immutable_browser_attribution(self):
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        migration = load_migrations(directory)[4]
        snapshot = tuple(sql_statements((directory.parent / "schema.sql").read_text()))
        for statement in migration.statements[2:]:
            self.assertIn(statement, snapshot)
        requests = next(statement for statement in migration.statements if statement.startswith("CREATE TABLE scan_app_requests"))
        self.assertIn("PRIMARY KEY (request_id)", requests)
        self.assertIn("browser_hash BINARY(32) NOT NULL", requests)
        self.assertIn("scanner_code VARCHAR(64) NOT NULL", requests)
        self.assertNotIn("REFERENCES serving_requests", requests)
        for action in ("UPDATE", "DELETE"):
            trigger = next(statement for statement in migration.statements if f"BEFORE {action} ON scan_app_requests" in statement)
            self.assertIn("SIGNAL SQLSTATE '45000'", trigger)
        settings = next(statement for statement in migration.statements if statement.startswith("CREATE TABLE scan_app_settings"))
        self.assertIn("CHECK (id = 1)", settings)

    def test_rejects_unrecognized_filename(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "migration.sql").write_text("SELECT 1;\n")
            with self.assertRaisesRegex(DomainError, "FILENAME"):
                load_migrations(root)

    def test_checksum_uses_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            data = b"SELECT 1;\n"
            Path(root, "001_initial.sql").write_bytes(data)
            self.assertEqual(load_migrations(root)[0].checksum, hashlib.sha256(data).hexdigest())


class MigrationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.migrations = (
            Migration(1, "initial", "a" * 64, ("SELECT 1",)),
            Migration(2, "next", "b" * 64, ("SELECT 2",)),
        )

    def row(self, version=1, checksum=None, status="APPLIED"):
        migration = self.migrations[version - 1]
        return {"version": version, "name": migration.name, "checksum": checksum or migration.checksum, "status": status}

    def test_applies_only_pending_versions(self):
        self.assertEqual(validate_ledger(self.migrations, [self.row()]), (self.migrations[1],))

    def test_modified_applied_migration_rejected(self):
        with self.assertRaisesRegex(DomainError, "CHECKSUM_MISMATCH"):
            validate_ledger(self.migrations, [self.row(checksum="c" * 64)])

    def test_incomplete_migration_requires_review(self):
        with self.assertRaisesRegex(DomainError, "INCOMPLETE_MIGRATION"):
            validate_ledger(self.migrations, [self.row(status="APPLYING")])

    def test_newer_unknown_database_version_rejected(self):
        with self.assertRaisesRegex(DomainError, "UNKNOWN_TO_APPLICATION"):
            validate_ledger(self.migrations, [{"version": 3}])

    def test_ledger_gap_rejected(self):
        with self.assertRaisesRegex(DomainError, "LEDGER_NOT_CONTIGUOUS"):
            validate_ledger(self.migrations, [self.row(version=2)])

    def test_runner_construction_does_not_connect(self):
        database = Mock()
        directory = Path(__file__).resolve().parents[2] / "database" / "migrations"
        MigrationRunner(database, directory)
        database._connect.assert_not_called()


class CliSafetyTests(unittest.TestCase):
    def test_database_commands_require_explicit_flags_before_database_creation(self):
        commands = [
            ["migrate"],
            ["schema-inspect"],
            ["bootstrap-admin"],
            ["baseline", "--version", "1", "--expected-schema-fingerprint", "a" * 64],
        ]
        for command in commands:
            with self.subTest(command=command):
                with patch("meal_management.cli.Database") as database:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(command), 1)
                    database.assert_not_called()

    def test_local_migration_plan_does_not_connect(self):
        with patch("meal_management.cli.Database") as database:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["migration-plan"]), 0)
            self.assertIn("initial", output.getvalue())
            database.assert_not_called()

    def test_bootstrap_refuses_noninteractive_input(self):
        with patch("meal_management.cli.Settings.from_env"):
            with patch("meal_management.cli.Database") as database:
                with patch("meal_management.cli.sys.stdin.isatty", return_value=False):
                    with contextlib.redirect_stderr(io.StringIO()) as output:
                        self.assertEqual(main(["bootstrap-admin", "--allow-database-access"]), 1)
                self.assertIn("INTERACTIVE_TERMINAL", output.getvalue())
                database.return_value._connect.assert_not_called()

    def test_database_errors_are_redacted(self):
        with patch("meal_management.cli.Settings.from_env", side_effect=RuntimeError("password=secret")):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main(["migrate", "--allow-database-changes"]), 1)
        self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
