import contextlib
import copy
import io
import secrets
import threading
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from meal_management.cli import main
from meal_management.config import Settings
from meal_management.development_seed import DevelopmentSeedService, EMPLOYEES, EXPECTED_KEYS, MASTER_LABEL, PinnedDatabase, SEED_VERSION, SeedResult, WAITERS, _EXPIRING_FIXTURE, _SeedQrService
from meal_management.employees import Registration
from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.qr import IssuedQr, QrService
from meal_management.runtime import RuntimeSettings
from meal_management.security import TokenVault, generate_token, token_digest


class SeedTransaction:
    def __init__(self, state, conflict=False):
        self.state = state
        self.conflict = conflict

    def now(self):
        return datetime(2026, 9, 9, 12, 0)

    def one(self, sql, params=()):
        if sql.startswith("SELECT name FROM system_locks"):
            return {"name": "bootstrap"}
        if sql.startswith("SELECT seed_version FROM development_seed_batches"):
            return {"seed_version": self.state["batch"]} if self.state["batch"] else None
        if sql.startswith("SELECT r.token_ciphertext"):
            row = next((row for row in self.state["records"] if row["qr_id"] == params[1]), None)
            if row is None:
                return None
            qr = self.state["qrs"][row["qr_id"]]
            return {**row, "token_hash": qr["token_hash"], "kind": qr["kind"], "employee_id": qr["employee_id"]}
        if sql.startswith("SELECT q.id AS qr_id, r.label"):
            row = next(row for row in self.state["records"] if row["entity_type"] == "MASTER")
            return {"qr_id": row["qr_id"], "label": row["label"]}
        if sql.startswith("SELECT id FROM"):
            return {"id": 999} if self.conflict else None
        raise AssertionError("Unexpected seed lookup")

    def all(self, sql, params=()):
        if sql.startswith("SELECT fixture_key"):
            return copy.deepcopy(self.state["records"])
        if sql.startswith("SELECT e.id AS employee_id"):
            return [
                {
                    "employee_id": row["entity_id"], "employee_code": row["fixture_key"],
                    "full_name": row["label"], "scenario": row["scenario"],
                    "qr_id": row["qr_id"], "expected_code": row["expected_code"],
                }
                for row in self.state["records"] if row["entity_type"] == "EMPLOYEE"
            ]
        if sql.startswith("SELECT s.email, s.display_name"):
            return [{"email": item["email"], "display_name": item["display_name"]} for item in self.state["waiters"]]
        raise AssertionError("Unexpected seed listing")

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO development_seed_batches"):
            self.state["batch"] = params[0]
            return 1
        if sql.startswith("INSERT INTO development_seed_records"):
            names = ("seed_version", "fixture_key", "entity_type", "entity_id", "label", "scenario", "expected_code", "qr_id", "token_ciphertext", "created_by")
            self.state["records"].append(dict(zip(names, params)))
            return 1
        raise AssertionError("Unexpected seed statement")


class MemorySeedDatabase:
    def __init__(self):
        self.state = {"batch": None, "records": [], "employees": [], "waiters": [], "qrs": {}, "emails": [], "calls": []}
        self.lock = threading.Lock()
        self.commits = 0
        self.rollbacks = 0
        self.conflict = False
        self.fail_commit = False

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            working = copy.deepcopy(self.state)
            try:
                yield SeedTransaction(working, self.conflict)
                if self.fail_commit:
                    raise RuntimeError("fictional commit failure")
            except BaseException:
                self.rollbacks += 1
                raise
            else:
                self.state = working
                self.commits += 1


class FakeSeedQr:
    def __init__(self, database, vault, renderer):
        self.database = database
        self.vault = vault

    def create(self, context, kind, employee_id=None, expires_at=None):
        with self.database.transaction() as tx:
            if expires_at is _EXPIRING_FIXTURE:
                expires_at = (tx.now() + timedelta(seconds=5)).replace(tzinfo=timezone.utc)
            token = generate_token()
            identifier = len(tx.state["qrs"]) + 100
            tx.state["qrs"][identifier] = {
                "token": token, "token_hash": token_digest(token), "kind": kind,
                "employee_id": employee_id, "expires_at": expires_at,
                "token_ciphertext": self.vault.encrypt(token), "revoked": False,
            }
            return IssuedQr(identifier, token)

    def issue_master(self, context, **values):
        return self.create(context, "MASTER")

    def retrieve(self, context, qr_id):
        with self.database.transaction() as tx:
            return IssuedQr(qr_id, tx.state["qrs"][qr_id]["token"])

    def _lock_credential(self, tx, qr_id):
        row = tx.state["qrs"][qr_id]
        employee = next((item for item in tx.state["employees"] if item["id"] == row["employee_id"]), None)
        return {"id": qr_id, **row}, employee

    def _decrypt_token(self, credential):
        return QrService._decrypt_token(self, credential)

    def revoke(self, context, qr_id, reason):
        with self.database.transaction() as tx:
            tx.state["qrs"][qr_id]["revoked"] = True
            tx.state["qrs"][qr_id]["token_ciphertext"] = None


class FakeSeedEmployee:
    def __init__(self, database, qr):
        self.database = database
        self.qr = qr

    def create_department(self, context, name):
        with self.database.transaction() as tx:
            tx.state["calls"].append(("department", name))
            return 50

    def register(self, context, **values):
        with self.database.transaction() as tx:
            identifier = len(tx.state["employees"]) + 1
            credential = self.qr.create(context, "EMPLOYEE", identifier, values["expires_at"])
            tx.state["employees"].append({"id": identifier, "is_active": True, **values, "expires_at": tx.state["qrs"][credential.qr_id]["expires_at"]})
            tx.state["emails"].append({"employee_id": identifier, "qr_id": credential.qr_id})
            return Registration(identifier, credential.qr_id, len(tx.state["emails"]))

    def set_active(self, context, employee_id, is_active):
        with self.database.transaction() as tx:
            tx.state["employees"][employee_id - 1]["is_active"] = is_active


class FakeSeedStaff:
    def __init__(self, database):
        self.database = database

    def create_staff(self, context, display_name, email, password, roles):
        with self.database.transaction() as tx:
            tx.state["waiters"].append({"display_name": display_name, "email": email, "roles": roles})
            return len(tx.state["waiters"]) + 70


class FakeSeedCatalog:
    def __init__(self, database):
        self.database = database

    def create_location(self, context, code, name):
        with self.database.transaction() as tx:
            tx.state["calls"].append(("location", code, name))
            return 60

    def create_meal_type(self, context, code, name):
        with self.database.transaction() as tx:
            tx.state["calls"].append(("meal_type", code, name))
            return 61

    def create_scanner(self, context, code, name, location_id):
        with self.database.transaction() as tx:
            tx.state["calls"].append(("scanner", code, name, location_id))
            return 80 + len(tx.state["calls"])


class DevelopmentSeedTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeSettings(csrf_secret=secrets.token_bytes(32), login_rate_secret=secrets.token_bytes(32))
        self.database = MemorySeedDatabase()
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.renderer = Mock(return_value=None)
        self.renderer.render.return_value = "<svg></svg>"
        self.context = ServerContext(generate_token())
        self.passwords = (secrets.token_urlsafe(24), secrets.token_urlsafe(24))
        self.service = DevelopmentSeedService(self.database, self.runtime, self.vault, self.renderer)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.actor = self.stack.enter_context(patch("meal_management.development_seed.require_actor", return_value=Actor(1, frozenset({"ADMIN"}))))
        self.audit = self.stack.enter_context(patch("meal_management.development_seed.audit"))
        self.stack.enter_context(patch("meal_management.development_seed._SeedQrService", FakeSeedQr))
        self.stack.enter_context(patch("meal_management.development_seed.EmployeeService", FakeSeedEmployee))
        self.stack.enter_context(patch("meal_management.development_seed.StaffService", FakeSeedStaff))
        self.stack.enter_context(patch("meal_management.development_seed.CatalogService", FakeSeedCatalog))

    def test_constructor_does_not_connect_or_write(self):
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.state["records"], [])

    def test_development_configuration_guards_precede_database_access(self):
        for runtime in (
            replace(self.runtime, environment="production"),
            replace(self.runtime, email_backend="ses"),
            replace(self.runtime, email_send_enabled=True),
        ):
            with self.subTest(runtime=runtime):
                database = Mock()
                service = DevelopmentSeedService(database, runtime, self.vault, self.renderer)
                for operation in (lambda: service.seed(self.context, self.passwords), lambda: service.manifest(self.context), lambda: service.export_svg(self.context, 100)):
                    with self.assertRaisesRegex(DomainError, "DEVELOPMENT_SEED_DISABLED"):
                        operation()
                database.transaction.assert_not_called()

    def test_seed_creates_fictional_owned_records_in_one_outer_commit(self):
        result = self.service.seed(self.context, self.passwords)
        self.assertTrue(result.created)
        self.assertEqual(result.expiry_wait_seconds, 6)
        self.assertEqual(self.database.commits, 1)
        state = self.database.state
        self.assertEqual(len(state["employees"]), 10)
        self.assertEqual(len(state["emails"]), 10)
        self.assertEqual(len(state["waiters"]), 2)
        self.assertEqual({record["fixture_key"] for record in state["records"]}, EXPECTED_KEYS)
        self.assertTrue(all(item["employee_code"].startswith("TEST-") for item in state["employees"]))
        self.assertTrue(all(item["email"].endswith("@example.test") for item in state["employees"] + state["waiters"]))
        self.assertTrue(all(item["roles"] == ["WAITER"] for item in state["waiters"]))
        self.assertEqual({item["kind"] for item in state["qrs"].values()}, {"EMPLOYEE", "MASTER"})

    def test_fixture_scenarios_use_deactivation_revocation_and_future_expiry(self):
        self.service.seed(self.context, self.passwords)
        state = self.database.state
        self.assertFalse(state["employees"][7]["is_active"])
        revoked = next(row for row in state["records"] if row["scenario"] == "REVOKED")
        self.assertTrue(state["qrs"][revoked["qr_id"]]["revoked"])
        self.assertIsNone(state["qrs"][revoked["qr_id"]]["token_ciphertext"])
        expiry = state["employees"][9]["expires_at"]
        self.assertEqual(expiry, datetime(2026, 9, 9, 12, 0, 5, tzinfo=timezone.utc))

    def test_seed_archives_without_calling_live_qr_retrieval(self):
        with patch.object(FakeSeedQr, "retrieve", side_effect=DomainError("QR_EXPIRED")) as retrieve:
            result = self.service.seed(self.context, self.passwords)
        self.assertTrue(result.created)
        retrieve.assert_not_called()

    def test_fresh_owned_credential_can_be_archived_after_expiry_during_render(self):
        token = generate_token()
        now = datetime(2026, 9, 9, 12, 0, 20)
        credential = {
            "id": 101, "kind": "EMPLOYEE", "employee_id": 7,
            "token_ciphertext": self.vault.encrypt(token), "token_hash": token_digest(token),
            "expires_at": now - timedelta(seconds=10), "revoked_at": None,
        }
        employee = {"id": 7, "full_name": "TEST Name", "email": "test@example.test", "is_active": True}
        tx = Mock()
        tx.one.side_effect = [{"employee_id": 7}, employee, credential]
        tx.now.return_value = now
        qr = QrService(Mock(), self.vault, self.renderer)
        archived = self.service._archive_registered(tx, qr, Registration(7, 101, 1))
        self.assertEqual(archived.token, token)
        with self.assertRaisesRegex(DomainError, "QR_EXPIRED"):
            qr._validate_current(tx, credential, employee)

    def test_fresh_archive_rejects_wrong_employee_ownership(self):
        qr = Mock()
        qr._lock_credential.return_value = ({"id": 101, "kind": "EMPLOYEE", "employee_id": 8}, {"id": 8})
        with self.assertRaisesRegex(DomainError, "REGISTRY_INCONSISTENT"):
            self.service._archive_registered(Mock(), qr, Registration(7, 101, 1))
        qr._decrypt_token.assert_not_called()

    def test_rerun_preserves_all_records_without_passwords(self):
        self.service.seed(self.context, self.passwords)
        before = copy.deepcopy(self.database.state)
        result = self.service.seed(self.context)
        self.assertFalse(result.created)
        self.assertEqual(result.expiry_wait_seconds, 0)
        self.assertEqual(self.database.state, before)

    def test_existing_unowned_namespace_collision_is_not_overwritten(self):
        self.database.conflict = True
        with self.assertRaisesRegex(DomainError, "DEVELOPMENT_SEED_NAMESPACE_CONFLICT"):
            self.service.seed(self.context, self.passwords)
        self.assertEqual(self.database.state["records"], [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)

    def test_new_seed_requires_explicit_waiter_passwords(self):
        for passwords in (None, [], ["short", "short"], (self.passwords[0],)):
            with self.subTest(passwords_present=passwords is not None):
                with self.assertRaisesRegex(DomainError, "DEVELOPMENT_WAITER_PASSWORDS_REQUIRED"):
                    self.service.seed(self.context, passwords)
        self.assertEqual(self.database.state["records"], [])

    def test_failed_commit_returns_no_success_and_preserves_original_state(self):
        self.database.fail_commit = True
        with self.assertRaisesRegex(RuntimeError, "fictional commit failure"):
            self.service.seed(self.context, self.passwords)
        self.assertEqual(self.database.state["employees"], [])
        self.assertIsNone(self.database.state["batch"])
        self.assertEqual(self.database.commits, 0)

    def test_mid_registration_failure_rolls_back_whole_seed(self):
        original = FakeSeedEmployee.register

        def failing_register(service, context, **values):
            if values["employee_code"] == "TEST-EMP-005":
                raise DomainError("FICTIONAL_REGISTRATION_FAILURE")
            return original(service, context, **values)

        with patch.object(FakeSeedEmployee, "register", failing_register):
            with self.assertRaisesRegex(DomainError, "FICTIONAL_REGISTRATION_FAILURE"):
                self.service.seed(self.context, self.passwords)
        self.assertEqual(self.database.state["waiters"], [])
        self.assertEqual(self.database.state["emails"], [])
        self.assertEqual(self.database.state["qrs"], {})

    def test_simultaneous_seed_calls_with_fake_serialized_transactions_do_not_duplicate(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service.seed(self.context, self.passwords), range(2)))
        self.assertEqual(sorted(result.created for result in results), [False, True])
        self.assertEqual(len(self.database.state["employees"]), 10)
        self.assertEqual(len(self.database.state["emails"]), 10)

    def test_incomplete_ownership_registry_is_rejected_without_repair(self):
        self.database.state["batch"] = SEED_VERSION
        with self.assertRaisesRegex(DomainError, "REGISTRY_INCONSISTENT"):
            self.service.seed(self.context, self.passwords)
        self.assertEqual(self.database.state["records"], [])

    def test_manifest_returns_metadata_without_credentials(self):
        self.service.seed(self.context, self.passwords)
        manifest = self.service.manifest(self.context)
        self.assertEqual(len(manifest["employees"]), 10)
        self.assertEqual(manifest["master"]["label"], MASTER_LABEL)
        self.assertEqual(len(manifest["waiters"]), 2)
        for credential in self.database.state["qrs"].values():
            self.assertNotIn(credential["token"], repr(manifest))
        self.assertNotIn("token_ciphertext", repr(manifest))

    def test_manifest_locks_ownership_before_actor_and_registry_reads(self):
        events = []
        original_one = SeedTransaction.one

        def lookup(tx, sql, params=()):
            if sql.startswith("SELECT name FROM system_locks"):
                events.append("lock")
            elif sql.startswith("SELECT seed_version FROM development_seed_batches"):
                events.append("registry")
            return original_one(tx, sql, params)

        def authenticate(*args):
            events.append("actor")
            return Actor(1, frozenset({"ADMIN"}))

        self.actor.side_effect = authenticate
        with patch.object(SeedTransaction, "one", lookup):
            result = self.service.manifest(self.context)
        self.assertEqual(events[:3], ["lock", "actor", "registry"])
        self.assertEqual(result, {"employees": [], "master": None, "waiters": []})

    def test_manifest_refuses_missing_ownership_lock_before_authentication(self):
        with patch.object(SeedTransaction, "one", return_value=None):
            with self.assertRaisesRegex(DomainError, "DATABASE_NOT_INITIALIZED"):
                self.service.manifest(self.context)
        self.actor.assert_not_called()

    def test_archived_revoked_and_expired_credentials_can_be_rendered_only_from_registry(self):
        self.service.seed(self.context, self.passwords)
        for scenario in ("REVOKED", "EXPIRED"):
            record = next(row for row in self.database.state["records"] if row["scenario"] == scenario)
            token = self.database.state["qrs"][record["qr_id"]]["token"]
            self.assertNotIn(token.encode(), record["token_ciphertext"])
            self.assertEqual(self.service.export_svg(self.context, record["qr_id"]), "<svg></svg>")
            self.renderer.render.assert_called_with(token)
        with self.assertRaisesRegex(DomainError, "DEVELOPMENT_QR_NOT_FOUND"):
            self.service.export_svg(self.context, 9999)

    def test_archived_token_hash_mismatch_is_rejected(self):
        self.service.seed(self.context, self.passwords)
        record = next(row for row in self.database.state["records"] if row["qr_id"] is not None)
        record["token_ciphertext"] = self.vault.encrypt(generate_token())
        with self.assertRaisesRegex(DomainError, "QR_TOKEN_MISMATCH"):
            self.service.export_svg(self.context, record["qr_id"])

    def test_archived_token_cannot_be_reassigned_to_another_employee(self):
        self.service.seed(self.context, self.passwords)
        record = next(row for row in self.database.state["records"] if row["entity_type"] == "EMPLOYEE")
        record["entity_id"] = 99999
        with self.assertRaisesRegex(DomainError, "REGISTRY_INCONSISTENT"):
            self.service.export_svg(self.context, record["qr_id"])

    def test_unauthenticated_or_nonadmin_context_cannot_seed_or_export(self):
        self.actor.side_effect = DomainError("ROLE_REQUIRED")
        for operation in (lambda: self.service.seed(self.context, self.passwords), lambda: self.service.manifest(self.context), lambda: self.service.export_svg(self.context, 100)):
            with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
                operation()

    def test_missing_registry_schema_has_clear_safe_error(self):
        error = RuntimeError("fictional missing table")
        error.errno = 1146
        with patch.object(SeedTransaction, "one", side_effect=error):
            with self.assertRaisesRegex(DomainError, "DEVELOPMENT_SEED_SCHEMA_REQUIRED"):
                self.service.manifest(self.context)

    def test_pinned_transactions_never_commit_independently(self):
        transaction = Mock()
        pinned = PinnedDatabase(transaction)
        with pinned.transaction() as outer:
            with pinned.transaction() as inner:
                self.assertIs(inner, transaction)
                self.assertIs(outer, transaction)
        transaction.commit.assert_not_called()
        transaction.rollback.assert_not_called()


class DevelopmentSeedCliTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeSettings(csrf_secret=secrets.token_bytes(32), login_rate_secret=secrets.token_bytes(32))
        self.settings = Settings("127.0.0.1", 3306, "fictional_local", "fictional_app", secrets.token_urlsafe(32), (Fernet.generate_key().decode(),))
        self.password = secrets.token_urlsafe(24)
        self.context = ServerContext(generate_token())

    def invoke(self, *, allowed=True, runtime=None, interactive=True, target="127.0.0.1:3306/fictional_local", existing=False, prompt_effect=None, seed_effect=None, logout_effect=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("meal_management.cli._load_environment"))
            stack.enter_context(patch("meal_management.cli.RuntimeSettings.from_env", return_value=runtime or self.runtime))
            stack.enter_context(patch("meal_management.cli.Settings.from_env", return_value=self.settings))
            database = stack.enter_context(patch("meal_management.cli.Database"))
            staff = stack.enter_context(patch("meal_management.cli.StaffService"))
            staff.return_value.authenticate.return_value = self.context
            staff.return_value.logout.side_effect = logout_effect
            service = stack.enter_context(patch("meal_management.cli.DevelopmentSeedService"))
            service.return_value.manifest.return_value = {"employees": [], "master": None, "waiters": [{"email": "test.waiter.one@example.test"}] if existing else []}
            service.return_value.seed.return_value = SeedResult(not existing, 0 if existing else 6)
            service.return_value.seed.side_effect = seed_effect
            stack.enter_context(patch("meal_management.cli.TokenVault"))
            stack.enter_context(patch("meal_management.cli.QrRenderer"))
            stack.enter_context(patch("meal_management.cli.sys.stdin.isatty", return_value=interactive))
            input_mock = stack.enter_context(patch("builtins.input", side_effect=[target, "admin@example.test"]))
            password = stack.enter_context(patch("meal_management.cli.getpass.getpass", return_value=self.password, side_effect=prompt_effect))
            sleep = stack.enter_context(patch("meal_management.cli.time.sleep"))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            result = main(["seed-development", *( ["--allow-database-changes"] if allowed else [])])
        return SimpleNamespace(result=result, stdout=stdout.getvalue(), stderr=stderr.getvalue(), database=database, staff=staff, service=service, input=input_mock, password=password, sleep=sleep)

    def test_requires_approval_before_prompt_or_database(self):
        result = self.invoke(allowed=False)
        self.assertEqual(result.result, 1)
        result.input.assert_not_called()
        result.password.assert_not_called()
        result.database.assert_not_called()

    def test_production_and_real_email_modes_are_rejected_before_database(self):
        for runtime in (replace(self.runtime, environment="production"), replace(self.runtime, email_backend="ses"), replace(self.runtime, email_send_enabled=True)):
            result = self.invoke(runtime=runtime)
            self.assertEqual(result.result, 1)
            self.assertIn("DEVELOPMENT_SEED_DISABLED", result.stderr)
            result.database.assert_not_called()
            result.password.assert_not_called()

    def test_target_must_be_confirmed_before_database_and_passwords(self):
        result = self.invoke(target="other-database")
        self.assertEqual(result.result, 1)
        self.assertIn("DEVELOPMENT_SEED_TARGET_NOT_CONFIRMED", result.stderr)
        result.database.assert_not_called()
        result.password.assert_not_called()

    def test_interactive_terminal_is_required(self):
        result = self.invoke(interactive=False)
        self.assertEqual(result.result, 1)
        self.assertIn("DEVELOPMENT_SEED_REQUIRES_INTERACTIVE_TERMINAL", result.stderr)
        result.database.assert_not_called()

    def test_new_seed_authenticates_admin_and_prompts_for_two_waiters(self):
        result = self.invoke()
        self.assertEqual(result.result, 0, result.stderr)
        result.staff.return_value.authenticate.assert_called_once_with("admin@example.test", self.password)
        self.assertEqual(result.password.call_count, 5)
        result.service.return_value.seed.assert_called_once_with(self.context, waiter_passwords=(self.password, self.password))
        result.staff.return_value.logout.assert_called_once_with(self.context)
        result.sleep.assert_called_once_with(6)
        self.assertNotIn(self.password, result.stdout + result.stderr)
        self.assertNotIn(self.context.session_token, result.stdout + result.stderr)

    def test_rerun_does_not_prompt_for_or_reset_waiter_passwords(self):
        result = self.invoke(existing=True)
        self.assertEqual(result.result, 0)
        self.assertEqual(result.password.call_count, 1)
        result.service.return_value.seed.assert_called_once_with(self.context, waiter_passwords=None)
        result.sleep.assert_not_called()

    def test_logout_failure_cannot_mask_committed_seed_or_skip_expiry_wait(self):
        result = self.invoke(logout_effect=RuntimeError("private logout secret " + self.password))
        self.assertEqual(result.result, 0)
        self.assertIn("Development test data created.", result.stdout)
        result.sleep.assert_called_once_with(6)
        self.assertEqual(result.stderr, "Temporary setup session could not be closed; it will expire automatically.\n")
        self.assertNotIn(self.password, result.stdout + result.stderr)

    def test_seed_error_is_preserved_when_logout_also_fails(self):
        result = self.invoke(
            seed_effect=DomainError("DEVELOPMENT_SEED_NAMESPACE_CONFLICT"),
            logout_effect=RuntimeError("private logout secret " + self.password),
        )
        self.assertEqual(result.result, 1)
        self.assertEqual(result.stderr, "DEVELOPMENT_SEED_NAMESPACE_CONFLICT\n")
        self.assertNotIn("Development test data created.", result.stdout)
        self.assertNotIn(self.password, result.stdout + result.stderr)
        result.sleep.assert_not_called()

    def test_password_prompt_refuses_echo_fallback_before_connection(self):
        import getpass

        def unsafe_prompt(*args, **kwargs):
            warnings.warn("unsafe echo", getpass.GetPassWarning)
            return self.password

        result = self.invoke(prompt_effect=unsafe_prompt)
        self.assertEqual(result.result, 1)
        self.assertIn("SECURE_PASSWORD_PROMPT_UNAVAILABLE", result.stderr)
        result.database.assert_not_called()


class SeedRelativeExpiryTests(unittest.TestCase):
    def test_expiry_uses_actual_issue_timestamp_without_resampling_clock(self):
        issued_at = datetime(2026, 9, 9, 12)
        tx = Mock()
        tx.now.return_value = issued_at + timedelta(seconds=30)
        qr = _SeedQrService(Mock(), Mock(), Mock())
        expiry = qr._expiry(tx, _EXPIRING_FIXTURE, issued_at)
        self.assertEqual(expiry, issued_at + timedelta(seconds=5))
        tx.now.assert_not_called()

    def test_ordinary_expiry_values_still_use_existing_validation(self):
        issued_at = datetime(2026, 9, 9, 12)
        qr = _SeedQrService(Mock(), Mock(), Mock())
        tx = Mock()
        self.assertIsNone(qr._expiry(tx, None, issued_at))
        with self.assertRaisesRegex(DomainError, "INVALID_QR_EXPIRY"):
            qr._expiry(tx, issued_at.replace(tzinfo=timezone.utc), issued_at)


if __name__ == "__main__":
    unittest.main()
