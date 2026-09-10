import hmac
from dataclasses import dataclass, field

from .auth import audit, require_actor
from .email_queue import EmailQueueService, email_delivery_state
from .errors import DomainError
from .security import generate_token, required_text, token_digest, utc_naive


@dataclass(frozen=True)
class IssuedQr:
    qr_id: int
    token: str = field(repr=False)


class QrService:
    def __init__(self, db, vault, renderer):
        self.db = db
        self.vault = vault
        self.renderer = renderer
        self.email_queue = EmailQueueService(db, vault, renderer)

    def _expiry(self, tx, expires_at, issued_at=None):
        if expires_at is None:
            return None
        expiry = utc_naive(expires_at)
        if expiry <= (issued_at if issued_at is not None else tx.now()):
            raise DomainError("INVALID_QR_EXPIRY")
        return expiry

    def _issue(self, tx, actor_id, kind, employee_id, expires_at):
        if kind not in {"EMPLOYEE", "MASTER"}:
            raise DomainError("INVALID_QR_KIND")
        if (kind == "EMPLOYEE") != (employee_id is not None):
            raise DomainError("INVALID_QR_OWNER")
        token = generate_token()
        ciphertext = self.vault.encrypt(token)
        issued_at = tx.now()
        expiry = self._expiry(tx, expires_at, issued_at)
        qr_id = tx.insert(
            "INSERT INTO qr_credentials "
            "(kind, employee_id, token_hash, token_ciphertext, issued_by, issued_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                kind,
                employee_id,
                token_digest(token),
                ciphertext,
                actor_id,
                issued_at,
                expiry,
            ),
        )
        audit(
            tx,
            actor_id,
            "QR_ISSUED",
            "qr_credentials",
            qr_id,
            after={"kind": kind, "employee_id": employee_id, "expires_at": expiry},
        )
        return IssuedQr(qr_id=qr_id, token=token)

    def _lock_employee(self, tx, employee_id):
        if type(employee_id) is not int or employee_id < 1:
            raise DomainError("INVALID_EMPLOYEE_ID")
        employee = tx.one(
            "SELECT id, full_name, email, is_active FROM employees WHERE id = %s FOR UPDATE",
            (employee_id,),
        )
        if employee is None:
            raise DomainError("EMPLOYEE_NOT_FOUND")
        return employee

    def _active_employee_qr(self, tx, employee):
        if not employee["is_active"]:
            raise DomainError("EMPLOYEE_INACTIVE")
        credential = tx.one(
            "SELECT id, kind, employee_id, token_hash, token_ciphertext, "
            "expires_at, revoked_at FROM qr_credentials "
            "WHERE employee_id = %s AND revoked_at IS NULL FOR UPDATE",
            (employee["id"],),
        )
        if credential is None:
            raise DomainError("QR_NOT_FOUND")
        self._validate_current(tx, credential, employee)
        return credential

    def _lock_credential(self, tx, qr_id):
        if type(qr_id) is not int or qr_id < 1:
            raise DomainError("INVALID_QR_ID")
        reference = tx.one(
            "SELECT employee_id FROM qr_credentials WHERE id = %s",
            (qr_id,),
        )
        if reference is None:
            raise DomainError("QR_NOT_FOUND")
        employee = None
        if reference["employee_id"] is not None:
            employee = self._lock_employee(tx, reference["employee_id"])
        credential = tx.one(
            "SELECT id, kind, employee_id, token_hash, token_ciphertext, "
            "expires_at, revoked_at FROM qr_credentials WHERE id = %s FOR UPDATE",
            (qr_id,),
        )
        if credential is None:
            raise DomainError("QR_NOT_FOUND")
        return credential, employee

    def _validate_current(self, tx, credential, employee=None):
        if credential["revoked_at"] is not None:
            raise DomainError("QR_REVOKED")
        if credential["expires_at"] is not None and credential["expires_at"] <= tx.now():
            raise DomainError("QR_EXPIRED")
        if credential["kind"] == "EMPLOYEE":
            if employee is None or not employee["is_active"]:
                raise DomainError("EMPLOYEE_INACTIVE")

    def _decrypt_token(self, credential):
        if credential["token_ciphertext"] is None:
            raise DomainError("QR_TOKEN_UNAVAILABLE")
        token = self.vault.decrypt(credential["token_ciphertext"])
        if not hmac.compare_digest(token_digest(token), bytes(credential["token_hash"])):
            raise DomainError("QR_TOKEN_MISMATCH")
        return token

    def _revoke(self, tx, actor_id, credential, reason):
        if credential["revoked_at"] is not None:
            return
        now = tx.now()
        tx.execute(
            "UPDATE qr_credentials SET revoked_at = %s, revoked_by = %s, "
            "revocation_reason = %s, token_ciphertext = NULL WHERE id = %s",
            (now, actor_id, reason, credential["id"]),
        )
        self.email_queue.cancel_for_qr(tx, credential["id"])
        audit(
            tx,
            actor_id,
            "QR_REVOKED",
            "qr_credentials",
            credential["id"],
            after={"revoked_at": now, "reason": reason},
        )

    def issue_master(self, context, expires_at=None):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            result = self._issue(tx, actor.staff_id, "MASTER", None, expires_at)
        return result

    def revoke(self, context, qr_id, reason):
        reason = required_text(reason, "revocation_reason", 255)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            credential, _ = self._lock_credential(tx, qr_id)
            self._revoke(tx, actor.staff_id, credential, reason)

    def replace_employee(self, context, employee_id, expires_at=None):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee = self._lock_employee(tx, employee_id)
            if not employee["is_active"]:
                raise DomainError("EMPLOYEE_INACTIVE")
            previous = tx.one(
                "SELECT id, revoked_at FROM qr_credentials "
                "WHERE employee_id = %s AND revoked_at IS NULL FOR UPDATE",
                (employee_id,),
            )
            if previous is not None:
                self._revoke(tx, actor.staff_id, previous, "REPLACED")
            result = self._issue(tx, actor.staff_id, "EMPLOYEE", employee_id, expires_at)
            self.email_queue.enqueue_employee(tx, result.qr_id, employee, result.token)
        return result

    def issue_employee(self, context, employee_id, expires_at=None):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee = self._lock_employee(tx, employee_id)
            if not employee["is_active"]:
                raise DomainError("EMPLOYEE_INACTIVE")
            if tx.one(
                "SELECT id FROM qr_credentials WHERE employee_id = %s AND revoked_at IS NULL FOR UPDATE",
                (employee_id,),
            ) is not None:
                raise DomainError("EMPLOYEE_QR_ALREADY_EXISTS")
            result = self._issue(tx, actor.staff_id, "EMPLOYEE", employee_id, expires_at)
            self.email_queue.enqueue_employee(tx, result.qr_id, employee, result.token)
        return result

    def replace_master(self, context, qr_id, expires_at=None):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            credential, _ = self._lock_credential(tx, qr_id)
            if credential["kind"] != "MASTER":
                raise DomainError("MASTER_QR_REQUIRED")
            if credential["revoked_at"] is not None:
                raise DomainError("QR_REVOKED")
            self._revoke(tx, actor.staff_id, credential, "REPLACED")
            result = self._issue(tx, actor.staff_id, "MASTER", None, expires_at)
        return result

    def resend_employee(self, context, employee_id):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee = self._lock_employee(tx, employee_id)
            credential = self._active_employee_qr(tx, employee)
            previous = tx.one(
                "SELECT id, status, delivery_mode, last_error FROM email_queue "
                "WHERE qr_id = %s AND recipient_email = %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (credential["id"], employee["email"]),
            )
            if previous is not None:
                status, _ = email_delivery_state(previous)
                if status == "NEEDS_REVIEW":
                    raise DomainError("EMAIL_DELIVERY_NEEDS_REVIEW")
                if previous["delivery_mode"] == "SINGLE" and status in {"DRAFT", "QUEUED"}:
                    return previous["id"]
            token = self._decrypt_token(credential)
            email_id = self.email_queue.enqueue_employee(tx, credential["id"], employee, token)
            audit(
                tx,
                actor.staff_id,
                "QR_EMAIL_QUEUED",
                "email_queue",
                email_id,
                after={"employee_id": employee_id, "qr_id": credential["id"]},
            )
        return email_id

    def export_svg(self, context, qr_id):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            credential, employee = self._lock_credential(tx, qr_id)
            self._validate_current(tx, credential, employee)
            svg = self.renderer.render(self._decrypt_token(credential))
            audit(tx, actor.staff_id, "QR_EXPORTED", "qr_credentials", qr_id)
        return svg

    def retrieve(self, context, qr_id):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            credential, employee = self._lock_credential(tx, qr_id)
            self._validate_current(tx, credential, employee)
            result = IssuedQr(credential["id"], self._decrypt_token(credential))
            audit(tx, actor.staff_id, "QR_RETRIEVED", "qr_credentials", qr_id)
        return result
