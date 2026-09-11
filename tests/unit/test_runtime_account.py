import base64
import os
import secrets
import ssl
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dotenv import dotenv_values

from meal_management import runtime_account
from meal_management.config import Settings
from meal_management.errors import DomainError
from meal_management.migrations import load_migrations
from meal_management.runtime import RuntimeSettings


INSERT_TABLES = {
    "departments", "employees", "staff_accounts", "staff_account_roles", "staff_sessions",
    "locations", "scanner_devices", "meal_types", "qr_credentials", "email_queue",
    "audit_events", "serving_requests", "visitor_authorizations", "servings", "meals",
    "scan_attempts", "scan_app_requests", "employee_email_batches", "login_rate_limits",
    "employee_archives", "meal_voids",
}
UPDATE_TABLES = {
    "employees", "staff_accounts", "staff_sessions", "locations", "scanner_devices",
    "qr_credentials", "email_queue", "serving_requests", "visitor_authorizations",
    "scan_attempts", "login_rate_limits", "scan_app_requests", "employee_email_batches",
    "system_locks",
}
MIGRATIONS = Path(__file__).resolve().parents[2] / "database" / "migrations"
PROVIDER_SECRET = "fictional-provider-secret-never-print"


def grant_rows():
    rows = [{"Grants": "GRANT USAGE ON *.* TO `meal_runtime`@`%`"}]
    rows.append({"Grants": "GRANT SELECT ON `defaultdb`.* TO `meal_runtime`@`%`"})
    for table in sorted(INSERT_TABLES | UPDATE_TABLES):
        privileges = [privilege for privilege, tables in (
            ("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES),
        ) if table in tables]
        rows.append({"Grants": "GRANT " + ", ".join(privileges) + " ON `defaultdb`.`" + table + "` TO `meal_runtime`@`%`"})
    return rows


class ProviderError(Exception):
    def __init__(self, errno=2003):
        self.errno = errno
        super().__init__(PROVIDER_SECRET)


class FakeSession:
    def __init__(self, fixture, connection):
        self.fixture = fixture
        self.connection = connection

    def one(self, sql, params=None):
        fixture = self.fixture
        name = self.connection.name
        fixture.calls.append((name, sql, params))
        if sql.startswith("SELECT GET_LOCK"):
            fixture.lock_attempts += 1
            return {"acquired": 0 if fixture.lock_failure == fixture.lock_attempts else 1}
        if sql.startswith("SELECT RELEASE_LOCK"):
            return {"released": 1}
        if sql.startswith("SELECT VERSION()"):
            return dict(fixture.server[name])
        if sql == "SHOW SESSION STATUS LIKE 'Ssl_cipher'":
            return {"Value": fixture.tls[name]}
        if sql == "SHOW CREATE USER CURRENT_USER()":
            return fixture.definition
        raise AssertionError("Unexpected query")

    def all(self, sql, params=None):
        fixture = self.fixture
        fixture.calls.append((self.connection.name, sql, params))
        if sql.startswith("SELECT version, name, checksum, status"):
            return fixture.ledger[self.connection.name]
        if sql == "SHOW GRANTS":
            if fixture.before_verification is not None:
                fixture.before_verification()
            return fixture.grants
        raise AssertionError("Unexpected query")

    def execute(self, sql, params=None):
        fixture = self.fixture
        fixture.calls.append((self.connection.name, sql, params))
        if sql.startswith("CREATE USER") and fixture.before_create is not None:
            fixture.before_create(params)
        if fixture.execute_failure and sql.startswith(fixture.execute_failure):
            raise fixture.error


class AccountFixture:
    def __init__(self, root):
        self.root = root
        self.ca = root / "fictional-ca.pem"
        self.ca.write_text("fictional certificate for mocked SSL validation", encoding="ascii")
        self.ca.chmod(0o600)
        self.output = root / "private" / "database-runtime.env"
        self.pending = self.output.with_suffix(".pending.env")
        self.settings = Settings(
            db_host="fictional-service.aivencloud.com", db_port=12345, db_name="defaultdb",
            db_user="avnadmin", db_password="fictional-owner-password",
            qr_encryption_keys=(base64.urlsafe_b64encode(b"q" * 32).decode(),),
            db_ssl_ca=str(self.ca),
        )
        self.runtime = RuntimeSettings(csrf_secret=b"c" * 32, login_rate_secret=b"l" * 32)
        self.calls = []
        self.connected_settings = []
        self.connections = []
        self.connect_failure = None
        self.commit_failure = False
        self.execute_failure = None
        self.error = ProviderError()
        self.lock_failure = None
        self.lock_attempts = 0
        self.before_create = None
        self.before_verification = None
        self.grants = grant_rows()
        self.definition = {
            "CREATE USER": "CREATE USER `meal_runtime`@`%` IDENTIFIED WITH 'caching_sha2_password' AS 'fictional-hash' REQUIRE SSL ACCOUNT UNLOCK",
        }
        server = {
            "version": "8.4.8", "name": "defaultdb", "active_role": "NONE",
            "mandatory_roles": "", "time_zone": "+00:00",
        }
        self.server = {
            "owner": dict(server, account="avnadmin@%"),
            "runtime": dict(server, account="meal_runtime@%"),
        }
        self.tls = {"owner": "TLS_AES_256_GCM_SHA384", "runtime": "TLS_AES_256_GCM_SHA384"}
        ledger = [{
            "version": item.version, "name": item.name, "checksum": item.checksum, "status": "APPLIED",
        } for item in load_migrations(MIGRATIONS)]
        self.ledger = {"owner": [dict(row) for row in ledger], "runtime": [dict(row) for row in ledger]}

    def database(self, settings):
        self.connected_settings.append(settings)
        name = "owner" if len(self.connected_settings) == 1 else "runtime"

        def connect():
            self.calls.append((name, "CONNECT", None))
            if self.connect_failure == name:
                raise self.error
            connection = Mock(name=name)
            connection.name = name

            def commit():
                self.calls.append((name, "COMMIT", None))
                if self.commit_failure:
                    raise self.error

            connection.commit.side_effect = commit
            self.connections.append(connection)
            return connection

        return SimpleNamespace(_connect=connect)

    def confirmation(self):
        return "CREATE meal_runtime@% ON " + self.settings.db_host + ":" + str(self.settings.db_port) + "/" + self.settings.db_name

    def run(self, **overrides):
        arguments = {"confirmation": self.confirmation(), "allow_database_changes": True}
        arguments.update(overrides)
        return runtime_account.create_aiven_runtime_account(
            self.settings, self.runtime, MIGRATIONS, self.output, **arguments,
        )


class RuntimeAccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = AccountFixture(Path(self.directory.name).resolve())
        self.database = patch.object(runtime_account, "Database", side_effect=self.fixture.database).start()
        self.addCleanup(patch.stopall)
        patch.object(runtime_account, "Session", side_effect=lambda connection: FakeSession(self.fixture, connection)).start()
        self.ssl_context = patch("meal_management.transfer.ssl.create_default_context").start()

    def assert_no_credentials_or_database(self):
        self.database.assert_not_called()
        self.assertFalse(self.fixture.pending.exists())
        self.assertFalse(self.fixture.output.exists())

    def assert_failed_safely(self, stage, code="RUNTIME_ACCOUNT_SETUP_FAILED", pending=True):
        with self.assertRaises(DomainError) as caught:
            self.fixture.run()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.runtime_account_stage, stage)
        self.assertEqual(error.runtime_account_pending_credentials, pending)
        self.assertEqual(self.fixture.pending.exists(), pending)
        self.assertFalse(self.fixture.output.exists())
        self.assertNotIn(PROVIDER_SECRET, str(error))
        self.assertNotIn(PROVIDER_SECRET, repr(error))
        self.assertTrue(error.__suppress_context__)
        for _, sql, _ in self.fixture.calls:
            self.assertFalse(sql.startswith(("DROP", "ALTER", "DELETE", "REVOKE")))
        for connection in self.fixture.connections:
            connection.close.assert_called_once()
        return error

    def test_explicit_approval_precedes_target_files_migrations_and_database(self):
        with patch.object(runtime_account, "target_confirmation") as target, patch.object(runtime_account, "_paths") as paths, patch.object(runtime_account, "load_migrations") as migrations:
            for value in (False, None, 1, "true"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(DomainError, "EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED"):
                        self.fixture.run(allow_database_changes=value)
            target.assert_not_called()
            paths.assert_not_called()
            migrations.assert_not_called()
        self.ssl_context.assert_not_called()
        self.assert_no_credentials_or_database()

    def test_exact_typed_target_precedes_credential_files_migrations_and_database(self):
        with patch.object(runtime_account, "_paths") as paths, patch.object(runtime_account, "load_migrations") as migrations:
            for value in (None, "yes", self.fixture.confirmation() + " ", "CREATE meal_runtime@% ON other:12345/defaultdb"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_TARGET_NOT_CONFIRMED"):
                        self.fixture.run(confirmation=value)
            paths.assert_not_called()
            migrations.assert_not_called()
        self.assert_no_credentials_or_database()

    def test_success_has_exact_parameterized_grants_and_no_schema_writes(self):
        result = self.fixture.run()
        self.assertEqual(result, {
            "status": "verified", "account": "meal_runtime@%", "credentials_file": str(self.fixture.output),
            "target": "fictional-service.aivencloud.com:12345/defaultdb",
        })
        statements = [(sql, params) for _, sql, params in self.fixture.calls if sql.startswith(("CREATE", "GRANT"))]
        create_sql, create_params = statements[0]
        self.assertEqual(create_sql, "CREATE USER %s@%s IDENTIFIED BY %s REQUIRE SSL")
        self.assertEqual(create_params[:2], ("meal_runtime", "%"))
        self.assertNotIn(create_params[2], create_sql)
        expected = {"GRANT SELECT ON `defaultdb`.* TO %s@%s"}
        for privilege, tables in (("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES)):
            expected.update("GRANT " + privilege + " ON `defaultdb`.`" + table + "` TO %s@%s" for table in tables)
        self.assertEqual({sql for sql, _ in statements[1:]}, expected)
        self.assertEqual(len(statements[1:]), len(expected))
        self.assertTrue(all(params == ("meal_runtime", "%") for _, params in statements[1:]))
        self.assertEqual(set(runtime_account.INSERT_TABLES), INSERT_TABLES)
        self.assertEqual(set(runtime_account.UPDATE_TABLES), UPDATE_TABLES)
        self.assertFalse(self.fixture.pending.exists())
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.fixture.output.parent.stat().st_mode), 0o700)
        self.assertEqual(len(self.fixture.connected_settings), 2)
        self.assertEqual(self.fixture.connected_settings[1].db_user, "meal_runtime")
        for connection in self.fixture.connections:
            connection.close.assert_called_once()

    def test_private_pending_credentials_precede_create_and_promotion_follows_verification(self):
        def check_create(params):
            self.assertTrue(self.fixture.pending.is_file())
            self.assertFalse(self.fixture.output.exists())
            self.assertEqual(stat.S_IMODE(self.fixture.pending.stat().st_mode), 0o600)
            self.assertEqual(dotenv_values(self.fixture.pending, interpolate=False)["DB_PASSWORD"], params[2])

        def check_verification():
            self.assertTrue(self.fixture.pending.exists())
            self.assertFalse(self.fixture.output.exists())

        self.fixture.before_create = check_create
        self.fixture.before_verification = check_verification
        self.fixture.run()
        self.assertTrue(self.fixture.output.exists())
        self.assertFalse(self.fixture.pending.exists())
        self.assertLess(self.fixture.calls.index(("owner", "COMMIT", None)), self.fixture.calls.index(("runtime", "CONNECT", None)))

    def test_password_uses_48_random_bytes_and_export_contains_only_database_credentials(self):
        original = secrets.token_urlsafe
        with patch.object(runtime_account.secrets, "token_urlsafe", wraps=original) as random_token:
            self.fixture.run()
        random_token.assert_called_once_with(48)
        candidate = self.fixture.connected_settings[1]
        self.assertGreaterEqual(len(candidate.db_password), 68)
        self.assertTrue(candidate.db_password.endswith("aA1!"))
        self.assertNotEqual(candidate.db_password, self.fixture.settings.db_password)
        values = dotenv_values(self.fixture.output, interpolate=False)
        self.assertEqual(set(values), {
            "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CONNECT_TIMEOUT", "DB_SSL_CA",
        })
        self.assertEqual(values["DB_PASSWORD"], candidate.db_password)
        self.assertEqual(candidate, replace(self.fixture.settings, db_user="meal_runtime", db_password=candidate.db_password))
        contents = self.fixture.output.read_text()
        for secret in (self.fixture.settings.db_password, *self.fixture.settings.qr_encryption_keys):
            self.assertNotIn(secret, contents)

    def test_preexisting_account_error_never_grants_or_retries(self):
        self.fixture.execute_failure = "CREATE USER"
        self.fixture.error = ProviderError(1396)
        error = self.assert_failed_safely("CREATE_ACCOUNT")
        self.assertEqual(error.runtime_account_mysql_error, 1396)
        self.assertEqual(sum(sql.startswith("CREATE USER") for _, sql, _ in self.fixture.calls), 1)
        self.assertFalse(any(sql.startswith("GRANT") for _, sql, _ in self.fixture.calls))
        self.assertEqual(len(self.fixture.connected_settings), 1)

    def test_grant_failure_preserves_pending_without_runtime_connection(self):
        self.fixture.execute_failure = "GRANT INSERT"
        self.assert_failed_safely("GRANTS")
        self.assertEqual(len(self.fixture.connected_settings), 1)
        self.fixture.connections[0].commit.assert_not_called()

    def test_commit_failure_preserves_pending_and_never_retries(self):
        self.fixture.commit_failure = True
        self.assert_failed_safely("GRANTS")
        self.fixture.connections[0].commit.assert_called_once()
        self.assertEqual(len(self.fixture.connected_settings), 1)

    def test_runtime_connection_failure_preserves_pending(self):
        self.fixture.connect_failure = "runtime"
        self.assert_failed_safely("VERIFY_ACCOUNT")
        self.assertEqual(len(self.fixture.connected_settings), 2)

    def test_owner_connection_failure_creates_no_credentials(self):
        self.fixture.connect_failure = "owner"
        self.assert_failed_safely("PREFLIGHT", pending=False)
        self.assertEqual(len(self.fixture.connected_settings), 1)

    def test_lock_contention_has_no_create_and_releases_only_acquired_lock(self):
        self.fixture.lock_failure = 2
        self.assert_failed_safely("PREFLIGHT", "RUNTIME_ACCOUNT_DATABASE_OPERATION_ALREADY_RUNNING", pending=False)
        releases = [params for _, sql, params in self.fixture.calls if sql.startswith("SELECT RELEASE_LOCK")]
        self.assertEqual(releases, [("meal-runtime-account",)])
        self.assertFalse(any(sql.startswith("CREATE") for _, sql, _ in self.fixture.calls))

    def test_runtime_tls_failure_preserves_pending(self):
        self.fixture.tls["runtime"] = ""
        self.assert_failed_safely("VERIFY_ACCOUNT", "RUNTIME_ACCOUNT_TLS_NOT_NEGOTIATED")

    def test_runtime_identity_roles_and_server_settings_must_match(self):
        variants = (
            ("account", "avnadmin@%", "RUNTIME_ACCOUNT_IDENTITY_OR_ROLES_MISMATCH"),
            ("active_role", "`privileged_role`@`%`", "RUNTIME_ACCOUNT_IDENTITY_OR_ROLES_MISMATCH"),
            ("mandatory_roles", "privileged_role", "RUNTIME_ACCOUNT_MANDATORY_ROLES_NOT_ALLOWED"),
            ("time_zone", "SYSTEM", "RUNTIME_ACCOUNT_MYSQL_8_4_UTC_TARGET_REQUIRED"),
            ("name", "otherdb", "RUNTIME_ACCOUNT_MYSQL_8_4_UTC_TARGET_REQUIRED"),
            ("version", "8.0.40", "RUNTIME_ACCOUNT_MYSQL_8_4_UTC_TARGET_REQUIRED"),
        )
        for key, value, code in variants:
            with self.subTest(key=key):
                server = dict(self.fixture.server["runtime"], **{key: value})
                tx = Mock()
                tx.one.return_value = server
                with self.assertRaisesRegex(DomainError, code):
                    runtime_account._check_server(tx, self.fixture.settings, load_migrations(MIGRATIONS), new_account=True)
                tx.all.assert_not_called()

    def test_runtime_account_requires_explicit_ssl_and_unlocked_definition(self):
        self.fixture.definition = {"CREATE USER": "CREATE USER `meal_runtime`@`%` REQUIRE NONE ACCOUNT UNLOCK"}
        self.assert_failed_safely("VERIFY_ACCOUNT", "RUNTIME_ACCOUNT_SSL_REQUIREMENT_NOT_VERIFIED")
        tx = Mock()
        tx.one.return_value = {"CREATE USER": "CREATE USER `meal_runtime`@`%` REQUIRE SSL ACCOUNT LOCK"}
        with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_MUST_BE_UNLOCKED"):
            runtime_account._check_account(tx)
        tx.all.assert_not_called()

    def test_quoted_metadata_cannot_forge_ssl_or_unlocked_attributes(self):
        definitions = (
            (
                "CREATE USER `meal_runtime`@`%` REQUIRE NONE ACCOUNT UNLOCK COMMENT 'REQUIRE SSL ACCOUNT UNLOCK'",
                "RUNTIME_ACCOUNT_SSL_REQUIREMENT_NOT_VERIFIED",
            ),
            (
                'CREATE USER `meal_runtime`@`%` REQUIRE SSL ACCOUNT LOCK ATTRIBUTE \'{"state":"ACCOUNT UNLOCK"}\'',
                "RUNTIME_ACCOUNT_MUST_BE_UNLOCKED",
            ),
        )
        for definition, code in definitions:
            with self.subTest(code=code):
                tx = Mock()
                tx.one.return_value = {"CREATE USER": definition}
                with self.assertRaisesRegex(DomainError, code):
                    runtime_account._check_account(tx)
                tx.all.assert_not_called()

    def test_runtime_grant_mismatch_preserves_pending(self):
        self.fixture.grants.append({"Grants": "GRANT DELETE ON `defaultdb`.`employees` TO `meal_runtime`@`%`"})
        self.assert_failed_safely("VERIFY_ACCOUNT", "RUNTIME_ACCOUNT_GRANTS_MISMATCH")

    def test_grant_verifier_rejects_extra_global_role_owner_and_grant_option(self):
        additions = (
            {"Grants": "GRANT ALL PRIVILEGES ON *.* TO `meal_runtime`@`%`"},
            {"Grants": "GRANT `admin_role`@`%` TO `meal_runtime`@`%`"},
            {"Grants": "GRANT SELECT ON `defaultdb`.* TO `avnadmin`@`%`"},
            {"Grants": "GRANT SELECT ON `defaultdb`.* TO `meal_runtime`@`%` WITH GRANT OPTION"},
            {"Grants": "GRANT SELECT ON `mysql`.* TO `meal_runtime`@`%`"},
            {"Grants": "GRANT UPDATE ON `defaultdb`.`schema_migrations` TO `meal_runtime`@`%`"},
        )
        for addition in additions:
            with self.subTest(statement=addition):
                with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_GRANTS_MISMATCH"):
                    runtime_account._check_grants(grant_rows() + [addition])
        with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_GRANTS_MISMATCH"):
            runtime_account._check_grants(grant_rows()[1:])
        runtime_account._check_grants(grant_rows())

    def test_pending_migration_or_tampered_ledger_stops_before_account_creation(self):
        self.fixture.ledger["owner"] = self.fixture.ledger["owner"][:-1]
        self.assert_failed_safely("PREFLIGHT", "RUNTIME_ACCOUNT_MIGRATIONS_NOT_CURRENT", pending=False)
        self.assertFalse(any(sql.startswith("CREATE") for _, sql, _ in self.fixture.calls))
        migrations = load_migrations(MIGRATIONS)
        ledger = [dict(row) for row in self.fixture.ledger["runtime"]]
        ledger[0]["checksum"] = "0" * 64
        tx = Mock()
        tx.one.side_effect = [self.fixture.server["runtime"], {"Value": "TLS"}]
        tx.all.return_value = ledger
        with self.assertRaisesRegex(DomainError, "MIGRATION_CHECKSUM_MISMATCH"):
            runtime_account._check_server(tx, self.fixture.settings, migrations, new_account=True)

    def test_missing_reviewed_migration_stops_before_paths_and_database(self):
        migrations = load_migrations(MIGRATIONS)[:-1]
        with patch.object(runtime_account, "load_migrations", return_value=migrations), patch.object(runtime_account, "_paths") as paths:
            with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_REQUIRES_REVIEWED_MIGRATIONS_ONE_TO_SEVEN"):
                self.fixture.run()
            paths.assert_not_called()
        self.assert_no_credentials_or_database()

    def test_target_requires_aiven_defaultdb_migration_owner_and_verified_tls(self):
        variants = (
            (replace(self.fixture.settings, db_host="localhost"), "TRANSFER_REQUIRES_AIVEN_HOSTNAME"),
            (replace(self.fixture.settings, db_name="otherdb"), "RUNTIME_ACCOUNT_MIGRATION_OWNER_AND_DEFAULTDB_REQUIRED"),
            (replace(self.fixture.settings, db_user="meal_runtime"), "RUNTIME_ACCOUNT_MIGRATION_OWNER_AND_DEFAULTDB_REQUIRED"),
            (replace(self.fixture.settings, db_user="root"), "TRANSFER_INVALID_AIVEN_CONFIGURATION"),
            (replace(self.fixture.settings, db_ssl_ca=None), "TRANSFER_VERIFIED_TLS_REQUIRED"),
            (replace(self.fixture.settings, db_ssl_ca="relative-ca.pem"), "TRANSFER_VERIFIED_TLS_REQUIRED"),
        )
        for settings, code in variants:
            with self.subTest(code=code):
                with self.assertRaisesRegex(DomainError, code):
                    runtime_account.target_confirmation(settings, self.fixture.runtime)
        self.ssl_context.side_effect = ssl.SSLError("fictional certificate failure")
        with self.assertRaisesRegex(DomainError, "TRANSFER_VERIFIED_TLS_REQUIRED"):
            self.fixture.run()
        self.assert_no_credentials_or_database()

    def test_production_real_email_or_remote_origins_cannot_provision(self):
        for runtime in (
            replace(self.fixture.runtime, environment="production"),
            replace(self.fixture.runtime, email_backend="gmail"),
            replace(self.fixture.runtime, email_send_enabled=True),
            replace(self.fixture.runtime, email_auto_send_enabled=True),
            replace(self.fixture.runtime, photo_backend="vercel_blob"),
        ):
            with self.subTest(runtime=runtime):
                with self.assertRaisesRegex(DomainError, "TRANSFER_REQUIRES_LOCAL_DEVELOPMENT_WITH_EMAIL_DISABLED"):
                    runtime_account.target_confirmation(self.fixture.settings, runtime)
        remote = replace(self.fixture.runtime, app_origin="https://admin.example.test", allowed_hosts=("admin.example.test", "localhost"))
        with self.assertRaisesRegex(DomainError, "TRANSFER_REQUIRES_LOOPBACK_ORIGINS"):
            runtime_account.target_confirmation(self.fixture.settings, remote)
        self.assert_no_credentials_or_database()

    def test_existing_pending_or_output_credentials_are_never_overwritten(self):
        self.fixture.output.parent.mkdir(mode=0o700)
        for path in (self.fixture.pending, self.fixture.output):
            with self.subTest(path=path.name):
                path.write_text("fictional preserved credentials", encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_CREDENTIALS_ALREADY_EXIST_REVIEW_REQUIRED"):
                    self.fixture.run()
                self.assertEqual(path.read_text(), "fictional preserved credentials")
                path.unlink()
        self.database.assert_not_called()

    def test_symlink_paths_and_insecure_parent_are_refused(self):
        self.fixture.output.parent.mkdir(mode=0o700)
        destination = self.fixture.root / "unrelated.env"
        destination.write_text("fictional unrelated data", encoding="utf-8")
        for path in (self.fixture.output, self.fixture.pending):
            with self.subTest(path=path.name):
                path.symlink_to(destination)
                with self.assertRaisesRegex(DomainError, "BACKUP_UNSAFE_PATH"):
                    self.fixture.run()
                path.unlink()
        self.fixture.output.parent.chmod(0o755)
        with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_PRIVATE_DIRECTORY_REQUIRED"):
            self.fixture.run()
        self.assertEqual(destination.read_text(), "fictional unrelated data")
        self.assert_no_credentials_or_database()

    def test_wrong_filename_is_refused_before_directory_creation(self):
        self.fixture.output = self.fixture.root / "missing" / "other.env"
        with self.assertRaisesRegex(DomainError, "RUNTIME_ACCOUNT_INVALID_CREDENTIALS_FILENAME"):
            self.fixture.run()
        self.assertFalse(self.fixture.output.parent.exists())
        self.database.assert_not_called()

    def test_private_file_failure_prevents_create_and_preserves_recoverable_pending(self):
        def failed_save(path, settings):
            path.write_text("fictional incomplete credential file", encoding="utf-8")
            path.chmod(0o600)
            raise OSError(PROVIDER_SECRET)

        with patch.object(runtime_account, "_save_pending", side_effect=failed_save):
            self.assert_failed_safely("PRIVATE_CREDENTIALS")
        self.assertFalse(any(sql.startswith("CREATE") for _, sql, _ in self.fixture.calls))

    def test_directory_sync_failure_preserves_pending_before_any_account_write(self):
        with patch.object(runtime_account, "_sync_directory", side_effect=OSError(PROVIDER_SECRET)) as sync:
            self.assert_failed_safely("PRIVATE_CREDENTIALS")
        sync.assert_called_once_with(self.fixture.pending.parent)
        self.assertFalse(any(sql.startswith(("CREATE", "GRANT")) for _, sql, _ in self.fixture.calls))
        self.assertEqual(stat.S_IMODE(self.fixture.pending.stat().st_mode), 0o600)

    def test_directory_sync_orders_durable_pending_link_and_removal(self):
        states = []
        original = runtime_account._sync_directory

        def sync(path):
            states.append((self.fixture.pending.exists(), self.fixture.output.exists()))
            original(path)

        def before_create(params):
            self.assertEqual(states, [(True, False)])

        self.fixture.before_create = before_create
        with patch.object(runtime_account, "_sync_directory", side_effect=sync):
            self.fixture.run()
        self.assertEqual(states, [(True, False), (True, True), (False, True)])

    def test_promotion_failure_preserves_verified_pending_without_account_retry(self):
        with patch.object(runtime_account.os, "link", side_effect=OSError(PROVIDER_SECRET)):
            self.assert_failed_safely("PUBLISH_CREDENTIALS")
        self.assertEqual(sum(sql.startswith("CREATE USER") for _, sql, _ in self.fixture.calls), 1)
        self.assertEqual(len(self.fixture.connected_settings), 2)
