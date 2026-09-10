import hmac
from dataclasses import asdict, dataclass
from uuid import UUID

from .auth import audit, require_actor
from .errors import DomainError
from .security import normalize_email, payload_digest, required_text


_UNSET = object()


@dataclass(frozen=True)
class Registration:
    employee_id: int
    qr_id: int
    email_id: int


def _positive_id(value, field):
    if type(value) is not int or not 1 <= value <= 2**64 - 1:
        raise DomainError("INVALID_" + field.upper())
    return value


def _selfie_key(value):
    if value is None:
        return None
    value = required_text(value, "selfie_object_key", 512)
    if value.lower().startswith("data:"):
        raise DomainError("INVALID_SELFIE_OBJECT_KEY")
    return value


class EmployeeService:
    def __init__(self, db, qr_service):
        self.db = db
        self.qr_service = qr_service

    def _department(self, tx, department_id):
        _positive_id(department_id, "department_id")
        department = tx.one(
            "SELECT id, is_active FROM departments WHERE id = %s FOR SHARE",
            (department_id,),
        )
        if department is None or not department["is_active"]:
            raise DomainError("DEPARTMENT_UNAVAILABLE")

    def create_department(self, context, name):
        name = required_text(name, "department_name", 100)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            try:
                department_id = tx.insert(
                    "INSERT INTO departments (name, is_active) VALUES (%s, %s)",
                    (name, True),
                )
            except Exception as exc:
                if getattr(exc, "errno", None) == 1062:
                    raise DomainError("DEPARTMENT_NAME_EXISTS") from None
                raise
            audit(
                tx,
                actor.staff_id,
                "DEPARTMENT_CREATED",
                "departments",
                department_id,
                after={"name": name},
            )
        return department_id

    def register(
        self,
        context,
        employee_code,
        full_name,
        email,
        department_id,
        selfie_object_key=None,
        expires_at=None,
    ):
        employee_code = required_text(employee_code, "employee_code", 32)
        full_name = required_text(full_name, "full_name", 150)
        email = normalize_email(email)
        selfie_object_key = _selfie_key(selfie_object_key)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            result = self._register(
                tx, actor, employee_code, full_name, email, department_id,
                selfie_object_key=selfie_object_key, expires_at=expires_at,
            )
        return result

    def _register(self, tx, actor, employee_code, full_name, email, department_id, *, selfie_object_key=None, expires_at=None, bulk_batch_id=None):
        self._department(tx, department_id)
        now = tx.now()
        try:
            employee_id = tx.insert(
                "INSERT INTO employees "
                "(employee_code, full_name, email, department_id, is_active, "
                "selfie_object_key, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (employee_code, full_name, email, department_id, True, selfie_object_key, now, now),
            )
        except Exception as exc:
            if getattr(exc, "errno", None) == 1062:
                raise DomainError("EMPLOYEE_CODE_EXISTS") from None
            raise
        issued = self.qr_service._issue(tx, actor.staff_id, "EMPLOYEE", employee_id, expires_at)
        email_id = self.qr_service.email_queue.enqueue_employee(
            tx, issued.qr_id, {"id": employee_id, "full_name": full_name, "email": email},
            issued.token, bulk_batch_id=bulk_batch_id,
        )
        audit(
            tx, actor.staff_id, "EMPLOYEE_REGISTERED", "employees", employee_id,
            after={
                "employee_code": employee_code, "full_name": full_name, "email": email,
                "department_id": department_id, "selfie_object_key": selfie_object_key,
                "qr_id": issued.qr_id, "email_id": email_id,
            },
        )
        return Registration(employee_id, issued.qr_id, email_id)

    def _bulk_replay(self, tx, batch, fingerprint, expected_count):
        if not hmac.compare_digest(bytes(batch["request_hash"]), fingerprint):
            raise DomainError("IDEMPOTENCY_KEY_REUSED")
        rows = tx.all(
            "SELECT c.employee_id, q.qr_id, q.id AS email_id FROM email_queue q "
            "JOIN qr_credentials c ON c.id = q.qr_id WHERE q.bulk_batch_id = %s ORDER BY q.id",
            (batch["id"],),
        )
        if len(rows) != expected_count or len({row["employee_id"] for row in rows}) != expected_count:
            raise DomainError("EMPLOYEE_BATCH_UNCONFIRMED")
        return {"batch_id": batch["id"], "employees": rows, "replayed": True}

    def register_bulk(self, context, request_id, employees):
        try:
            identifier = request_id if isinstance(request_id, UUID) else UUID(str(request_id))
            if identifier.int == 0:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise DomainError("INVALID_REQUEST_ID") from None
        if not isinstance(employees, list) or not 1 <= len(employees) <= 100:
            raise DomainError("INVALID_EMPLOYEE_BATCH")
        normalized = []
        for employee in employees:
            if not isinstance(employee, dict) or set(employee) != {"employee_code", "full_name", "email", "department_id"}:
                raise DomainError("INVALID_EMPLOYEE_BATCH")
            normalized.append({
                "employee_code": required_text(employee["employee_code"], "employee_code", 32),
                "full_name": required_text(employee["full_name"], "full_name", 150),
                "email": normalize_email(employee["email"]),
                "department_id": _positive_id(employee["department_id"], "department_id"),
            })
        if len({row["employee_code"].casefold() for row in normalized}) != len(normalized):
            raise DomainError("EMPLOYEE_CODE_EXISTS")
        fingerprint = payload_digest({"employees": normalized})
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            parameters = (actor.staff_id, identifier.bytes)
            lookup = (
                "SELECT id, request_hash FROM employee_email_batches "
                "WHERE created_by_staff_id = %s AND request_id = %s FOR UPDATE"
            )
            batch = tx.one(lookup, parameters)
            if batch is not None:
                result = self._bulk_replay(tx, batch, fingerprint, len(normalized))
            else:
                try:
                    batch_id = tx.insert(
                        "INSERT INTO employee_email_batches (request_id, created_by_staff_id, request_hash, created_at) "
                        "VALUES (%s, %s, %s, %s)",
                        (identifier.bytes, actor.staff_id, fingerprint, tx.now()),
                    )
                except Exception as exc:
                    if getattr(exc, "errno", None) != 1062:
                        raise
                    batch = tx.one(lookup, parameters)
                    if batch is None:
                        raise DomainError("EMPLOYEE_BATCH_UNCONFIRMED") from None
                    result = self._bulk_replay(tx, batch, fingerprint, len(normalized))
                else:
                    registrations = [asdict(self._register(tx, actor, **employee, bulk_batch_id=batch_id)) for employee in normalized]
                    result = {"batch_id": batch_id, "employees": registrations, "replayed": False}
        return result

    def update(
        self,
        context,
        employee_id,
        *,
        employee_code=_UNSET,
        full_name=_UNSET,
        email=_UNSET,
        department_id=_UNSET,
        selfie_object_key=_UNSET,
    ):
        _positive_id(employee_id, "employee_id")
        supplied = {}
        if employee_code is not _UNSET:
            supplied["employee_code"] = required_text(employee_code, "employee_code", 32)
        if full_name is not _UNSET:
            supplied["full_name"] = required_text(full_name, "full_name", 150)
        if email is not _UNSET:
            supplied["email"] = normalize_email(email)
        if department_id is not _UNSET:
            supplied["department_id"] = _positive_id(department_id, "department_id")
        if selfie_object_key is not _UNSET:
            supplied["selfie_object_key"] = _selfie_key(selfie_object_key)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee = tx.one(
                "SELECT id, employee_code, full_name, email, department_id, "
                "selfie_object_key, is_active FROM employees WHERE id = %s FOR UPDATE",
                (employee_id,),
            )
            if employee is None:
                raise DomainError("EMPLOYEE_NOT_FOUND")
            if not supplied:
                return
            updated = dict(employee)
            updated.update(supplied)
            if "department_id" in supplied:
                self._department(tx, updated["department_id"])
            try:
                tx.execute(
                    "UPDATE employees SET employee_code = %s, full_name = %s, email = %s, "
                    "department_id = %s, selfie_object_key = %s, updated_at = %s WHERE id = %s",
                    (
                        updated["employee_code"],
                        updated["full_name"],
                        updated["email"],
                        updated["department_id"],
                        updated["selfie_object_key"],
                        tx.now(),
                        employee_id,
                    ),
                )
            except Exception as exc:
                if getattr(exc, "errno", None) == 1062:
                    raise DomainError("EMPLOYEE_CODE_EXISTS") from None
                raise
            if updated["email"] != employee["email"]:
                credential = tx.one(
                    "SELECT id, kind, employee_id, token_hash, token_ciphertext, "
                    "expires_at, revoked_at FROM qr_credentials "
                    "WHERE employee_id = %s AND revoked_at IS NULL FOR UPDATE",
                    (employee_id,),
                )
                if credential is not None:
                    self.qr_service.email_queue.cancel_for_qr(tx, credential["id"])
                    unexpired = (
                        credential["expires_at"] is None or credential["expires_at"] > tx.now()
                    )
                    if employee["is_active"] and unexpired:
                        token = self.qr_service._decrypt_token(credential)
                        self.qr_service.email_queue.enqueue_employee(
                            tx, credential["id"], updated, token
                        )
            audit(
                tx,
                actor.staff_id,
                "EMPLOYEE_UPDATED",
                "employees",
                employee_id,
                before={key: employee[key] for key in supplied},
                after=supplied,
            )

    def set_active(self, context, employee_id, is_active):
        _positive_id(employee_id, "employee_id")
        if type(is_active) is not bool:
            raise DomainError("INVALID_EMPLOYEE_STATUS")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee = self.qr_service._lock_employee(tx, employee_id)
            tx.execute(
                "UPDATE employees SET is_active = %s, updated_at = %s WHERE id = %s",
                (is_active, tx.now(), employee_id),
            )
            if not is_active:
                credential = tx.one(
                    "SELECT id FROM qr_credentials "
                    "WHERE employee_id = %s AND revoked_at IS NULL FOR UPDATE",
                    (employee_id,),
                )
                if credential is not None:
                    self.qr_service.email_queue.cancel_for_qr(tx, credential["id"])
            audit(
                tx,
                actor.staff_id,
                "EMPLOYEE_STATUS_CHANGED",
                "employees",
                employee_id,
                before={"is_active": bool(employee["is_active"])},
                after={"is_active": is_active},
            )
