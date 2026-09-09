import hmac
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta, timezone

from .accounts import StaffService
from .auth import audit, require_actor
from .catalog import CatalogService
from .database import retry_transaction
from .employees import EmployeeService
from .errors import DomainError
from .qr import IssuedQr, QrService
from .security import token_digest


SEED_VERSION = "local-test-v1"
DEPARTMENT_NAME = "TEST Development"
LOCATION_CODE = "TEST-CAFETERIA"
LOCATION_NAME = "TEST Cafeteria"
MEAL_TYPE_CODE = "TEST-LUNCH"
MEAL_TYPE_NAME = "TEST Lunch"
MASTER_LABEL = "TEST Admin Office"
WAITERS = (
    ("waiter-1", "TEST Waiter One", "test.waiter.one@example.test"),
    ("waiter-2", "TEST Waiter Two", "test.waiter.two@example.test"),
)
SCANNERS = (
    ("scanner-1", "TEST-SCANNER-A", "TEST Scanner A"),
    ("scanner-2", "TEST-SCANNER-B", "TEST Scanner B"),
)
EMPLOYEES = (
    ("TEST-EMP-001", "TEST Asha Rao", "ACTIVE", "APPROVED"),
    ("TEST-EMP-002", "TEST Dev Mehta", "ACTIVE", "APPROVED"),
    ("TEST-EMP-003", "TEST Mira Shah", "ACTIVE", "APPROVED"),
    ("TEST-EMP-004", "TEST Nikhil Bose", "ACTIVE", "APPROVED"),
    ("TEST-EMP-005", "TEST Priya Nair", "ACTIVE", "APPROVED"),
    ("TEST-EMP-006", "TEST Rahul Das", "ACTIVE", "APPROVED"),
    ("TEST-EMP-007", "TEST Sana Kapoor", "ACTIVE", "APPROVED"),
    ("TEST-EMP-008", "TEST Tara Sen", "INACTIVE", "EMPLOYEE_INACTIVE"),
    ("TEST-EMP-009", "TEST Vivek Jain", "REVOKED", "QR_REVOKED"),
    ("TEST-EMP-010", "TEST Zoya Patel", "EXPIRED", "QR_EXPIRED"),
)
EXPECTED_KEYS = frozenset(
    {"department", "location", "meal-type", "master"}
    | {item[0] for item in WAITERS}
    | {item[0] for item in SCANNERS}
    | {item[0] for item in EMPLOYEES}
)
_EXPIRING_FIXTURE = object()


class _SeedQrService(QrService):
    def _expiry(self, tx, expires_at, issued_at=None):
        if expires_at is _EXPIRING_FIXTURE:
            issued_at = tx.now() if issued_at is None else issued_at
            expires_at = (issued_at + timedelta(seconds=5)).replace(tzinfo=timezone.utc)
        return super()._expiry(tx, expires_at, issued_at)


def require_development_seed(runtime):
    if (
        runtime.environment != "development"
        or runtime.email_backend != "preview"
        or runtime.email_send_enabled
    ):
        raise DomainError("DEVELOPMENT_SEED_DISABLED")


@dataclass(frozen=True)
class SeedResult:
    created: bool
    expiry_wait_seconds: int = 0


class PinnedDatabase:
    def __init__(self, transaction):
        self.transaction_session = transaction

    @contextmanager
    def transaction(self):
        yield self.transaction_session


class DevelopmentSeedService:
    def __init__(self, database, runtime, vault, renderer):
        self.database = database
        self.runtime = runtime
        self.vault = vault
        self.renderer = renderer

    @contextmanager
    def _transaction(self):
        try:
            with self.database.transaction() as tx:
                yield tx
        except Exception as error:
            if getattr(error, "errno", None) in {1054, 1146}:
                raise DomainError("DEVELOPMENT_SEED_SCHEMA_REQUIRED") from None
            raise

    def _registry(self, tx):
        batch = tx.one(
            "SELECT seed_version FROM development_seed_batches WHERE seed_version = %s",
            (SEED_VERSION,),
        )
        rows = tx.all(
            "SELECT fixture_key, entity_type, entity_id, qr_id, label, scenario, expected_code "
            "FROM development_seed_records WHERE seed_version = %s ORDER BY fixture_key",
            (SEED_VERSION,),
        )
        if batch is None:
            if rows:
                raise DomainError("DEVELOPMENT_SEED_REGISTRY_INCONSISTENT")
            return None
        if {row["fixture_key"] for row in rows} != EXPECTED_KEYS:
            raise DomainError("DEVELOPMENT_SEED_REGISTRY_INCONSISTENT")
        return rows

    def _lock_ownership(self, tx):
        if tx.one("SELECT name FROM system_locks WHERE name = %s FOR UPDATE", ("bootstrap",)) is None:
            raise DomainError("DATABASE_NOT_INITIALIZED")

    def _archive_registered(self, tx, qr, registered):
        credential, employee = qr._lock_credential(tx, registered.qr_id)
        if (
            employee is None
            or employee["id"] != registered.employee_id
            or credential["id"] != registered.qr_id
            or credential["kind"] != "EMPLOYEE"
            or credential["employee_id"] != registered.employee_id
        ):
            raise DomainError("DEVELOPMENT_SEED_REGISTRY_INCONSISTENT")
        return IssuedQr(registered.qr_id, qr._decrypt_token(credential))

    def _preflight(self, tx):
        checks = (
            ("SELECT id FROM departments WHERE name = %s LIMIT 1", (DEPARTMENT_NAME,)),
            ("SELECT id FROM locations WHERE code = %s OR name = %s LIMIT 1", (LOCATION_CODE, LOCATION_NAME)),
            ("SELECT id FROM meal_types WHERE code = %s OR name = %s LIMIT 1", (MEAL_TYPE_CODE, MEAL_TYPE_NAME)),
        )
        for statement, parameters in checks:
            if tx.one(statement, parameters) is not None:
                raise DomainError("DEVELOPMENT_SEED_NAMESPACE_CONFLICT")
        for _, code, name in SCANNERS:
            if tx.one("SELECT id FROM scanner_devices WHERE code = %s OR name = %s LIMIT 1", (code, name)):
                raise DomainError("DEVELOPMENT_SEED_NAMESPACE_CONFLICT")
        for _, name, email in WAITERS:
            if tx.one("SELECT id FROM staff_accounts WHERE email = %s OR display_name = %s LIMIT 1", (email, name)):
                raise DomainError("DEVELOPMENT_SEED_NAMESPACE_CONFLICT")
        for code, name, _, _ in EMPLOYEES:
            email = code.lower() + "@example.test"
            if tx.one(
                "SELECT id FROM employees WHERE employee_code = %s OR email = %s OR full_name = %s LIMIT 1",
                (code, email, name),
            ):
                raise DomainError("DEVELOPMENT_SEED_NAMESPACE_CONFLICT")

    def _own(self, tx, actor_id, key, entity_type, entity_id, label, *, scenario=None, expected_code=None, credential=None):
        tx.execute(
            "INSERT INTO development_seed_records "
            "(seed_version, fixture_key, entity_type, entity_id, label, scenario, expected_code, "
            "qr_id, token_ciphertext, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                SEED_VERSION, key, entity_type, entity_id, label, scenario, expected_code,
                credential.qr_id if credential else None,
                self.vault.encrypt(credential.token) if credential else None,
                actor_id,
            ),
        )

    @retry_transaction
    def seed(self, context, waiter_passwords=None):
        require_development_seed(self.runtime)
        with self._transaction() as tx:
            self._lock_ownership(tx)
            actor = require_actor(tx, context, {"ADMIN"})
            if self._registry(tx) is not None:
                return SeedResult(created=False)
            self._preflight(tx)
            if (
                not isinstance(waiter_passwords, (tuple, list))
                or len(waiter_passwords) != len(WAITERS)
                or any(not isinstance(password, str) or not 12 <= len(password) <= 1024 for password in waiter_passwords)
            ):
                raise DomainError("DEVELOPMENT_WAITER_PASSWORDS_REQUIRED")
            tx.execute(
                "INSERT INTO development_seed_batches (seed_version, created_by) VALUES (%s, %s)",
                (SEED_VERSION, actor.staff_id),
            )
            pinned = PinnedDatabase(tx)
            staff = StaffService(pinned)
            qr = _SeedQrService(pinned, self.vault, self.renderer)
            employees = EmployeeService(pinned, qr)
            catalog = CatalogService(pinned)
            department_id = employees.create_department(context, DEPARTMENT_NAME)
            self._own(tx, actor.staff_id, "department", "DEPARTMENT", department_id, DEPARTMENT_NAME)
            location_id = catalog.create_location(context, LOCATION_CODE, LOCATION_NAME)
            self._own(tx, actor.staff_id, "location", "LOCATION", location_id, LOCATION_NAME)
            meal_type_id = catalog.create_meal_type(context, MEAL_TYPE_CODE, MEAL_TYPE_NAME)
            self._own(tx, actor.staff_id, "meal-type", "MEAL_TYPE", meal_type_id, MEAL_TYPE_NAME)
            for key, code, name in SCANNERS:
                scanner_id = catalog.create_scanner(context, code, name, location_id)
                self._own(tx, actor.staff_id, key, "SCANNER", scanner_id, name)
            for (key, name, email), password in zip(WAITERS, waiter_passwords):
                waiter_id = staff.create_staff(context, name, email, password, ["WAITER"])
                self._own(tx, actor.staff_id, key, "WAITER", waiter_id, name)
            master = qr.issue_master(context)
            self._own(tx, actor.staff_id, "master", "MASTER", master.qr_id, MASTER_LABEL, credential=master)
            for code, name, scenario, expected_code in EMPLOYEES:
                expiry = _EXPIRING_FIXTURE if scenario == "EXPIRED" else None
                registered = employees.register(
                    context, employee_code=code, full_name=name, email=code.lower() + "@example.test",
                    department_id=department_id, expires_at=expiry,
                )
                credential = self._archive_registered(tx, qr, registered)
                self._own(
                    tx, actor.staff_id, code, "EMPLOYEE", registered.employee_id, name,
                    scenario=scenario, expected_code=expected_code, credential=credential,
                )
                if scenario == "INACTIVE":
                    employees.set_active(context, registered.employee_id, False)
                elif scenario == "REVOKED":
                    qr.revoke(context, registered.qr_id, "TEST fixture revocation")
            audit(
                tx, actor.staff_id, "DEVELOPMENT_TEST_DATA_CREATED", "development_seed_batches", SEED_VERSION,
                after={"employee_count": len(EMPLOYEES), "waiter_count": len(WAITERS)},
            )
        return SeedResult(created=True, expiry_wait_seconds=6)

    def manifest(self, context):
        require_development_seed(self.runtime)
        with self._transaction() as tx:
            self._lock_ownership(tx)
            require_actor(tx, context, {"ADMIN"})
            records = self._registry(tx)
            if records is None:
                return {"employees": [], "master": None, "waiters": []}
            employees = tx.all(
                "SELECT e.id AS employee_id, e.employee_code, e.full_name, r.scenario, r.qr_id, r.expected_code "
                "FROM development_seed_records r JOIN employees e ON e.id = r.entity_id "
                "JOIN qr_credentials q ON q.id = r.qr_id AND q.employee_id = e.id AND q.kind = 'EMPLOYEE' "
                "WHERE r.seed_version = %s AND r.entity_type = 'EMPLOYEE' ORDER BY e.employee_code",
                (SEED_VERSION,),
            )
            master = tx.one(
                "SELECT q.id AS qr_id, r.label FROM development_seed_records r "
                "JOIN qr_credentials q ON q.id = r.qr_id AND q.id = r.entity_id AND q.kind = 'MASTER' "
                "WHERE r.seed_version = %s AND r.fixture_key = 'master' AND r.entity_type = 'MASTER'",
                (SEED_VERSION,),
            )
            waiters = tx.all(
                "SELECT s.email, s.display_name FROM development_seed_records r "
                "JOIN staff_accounts s ON s.id = r.entity_id "
                "WHERE r.seed_version = %s AND r.entity_type = 'WAITER' ORDER BY r.fixture_key",
                (SEED_VERSION,),
            )
            if len(employees) != len(EMPLOYEES) or master is None or len(waiters) != len(WAITERS):
                raise DomainError("DEVELOPMENT_SEED_REGISTRY_INCONSISTENT")
            return {"employees": employees, "master": master, "waiters": waiters}

    def export_svg(self, context, qr_id):
        require_development_seed(self.runtime)
        if type(qr_id) is not int or not 1 <= qr_id <= 2**64 - 1:
            raise DomainError("INVALID_QR_ID")
        with self._transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            credential = tx.one(
                "SELECT r.token_ciphertext, q.token_hash, r.entity_type, r.entity_id, q.kind, q.employee_id "
                "FROM development_seed_records r JOIN qr_credentials q ON q.id = r.qr_id "
                "WHERE r.seed_version = %s AND r.qr_id = %s AND r.entity_type IN ('EMPLOYEE', 'MASTER')",
                (SEED_VERSION, qr_id),
            )
            if credential is None:
                raise DomainError("DEVELOPMENT_QR_NOT_FOUND")
            owned = (
                credential["entity_type"] == "EMPLOYEE"
                and credential["kind"] == "EMPLOYEE"
                and credential["entity_id"] == credential["employee_id"]
            ) or (
                credential["entity_type"] == "MASTER"
                and credential["kind"] == "MASTER"
                and credential["entity_id"] == qr_id
                and credential["employee_id"] is None
            )
            if not owned:
                raise DomainError("DEVELOPMENT_SEED_REGISTRY_INCONSISTENT")
            token = self.vault.decrypt(credential["token_ciphertext"])
            if not hmac.compare_digest(token_digest(token), bytes(credential["token_hash"])):
                raise DomainError("QR_TOKEN_MISMATCH")
            svg = self.renderer.render(token)
            audit(tx, actor.staff_id, "DEVELOPMENT_QR_EXPORTED", "qr_credentials", qr_id)
        return svg
