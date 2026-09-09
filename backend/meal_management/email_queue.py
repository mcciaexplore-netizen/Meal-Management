import hmac
import json
from dataclasses import dataclass, field

from .auth import require_actor
from .errors import DomainError
from .security import required_text, token_digest


@dataclass(frozen=True)
class EmailPreview:
    email_id: int
    recipient_email: str
    employee_name: str
    token: str = field(repr=False)
    qr_svg: str = field(repr=False)


class EmailQueueService:
    def __init__(self, db, vault, renderer):
        self.db = db
        self.vault = vault
        self.renderer = renderer

    def enqueue_employee(self, tx, qr_id, employee, token):
        payload = json.dumps(
            {
                "employee_id": employee["id"],
                "employee_name": employee["full_name"],
                "token": token,
                "qr_svg": self.renderer.render(token),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = tx.now()
        return tx.insert(
            "INSERT INTO email_queue "
            "(qr_id, recipient_email, payload_ciphertext, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                qr_id,
                employee["email"],
                self.vault.encrypt(payload),
                "QUEUED",
                now,
                now,
            ),
        )

    def cancel_for_qr(self, tx, qr_id):
        tx.execute(
            "UPDATE email_queue SET "
            "status = CASE WHEN status IN ('QUEUED', 'FAILED') "
            "THEN 'CANCELLED' ELSE status END, "
            "payload_ciphertext = NULL, updated_at = %s, last_error = NULL "
            "WHERE qr_id = %s",
            (tx.now(), qr_id),
        )

    def pending(self, context, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DomainError("INVALID_LIMIT")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            return tx.all(
                "SELECT q.id, q.qr_id, q.recipient_email, q.status, q.created_at "
                "FROM email_queue AS q "
                "JOIN qr_credentials AS c ON c.id = q.qr_id "
                "JOIN employees AS e ON e.id = c.employee_id "
                "WHERE q.status = 'QUEUED' AND c.revoked_at IS NULL "
                "AND (c.expires_at IS NULL OR c.expires_at > %s) "
                "AND e.is_active = 1 AND e.email = q.recipient_email "
                "ORDER BY q.created_at, q.id LIMIT %s",
                (tx.now(), limit),
            )

    def lock_email(self, tx, email_id):
        if type(email_id) is not int or email_id < 1:
            raise DomainError("INVALID_EMAIL_ID")
        reference = tx.one(
            "SELECT c.employee_id, c.id AS qr_id FROM email_queue AS q "
            "JOIN qr_credentials AS c ON c.id = q.qr_id WHERE q.id = %s",
            (email_id,),
        )
        if reference is None:
            raise DomainError("EMAIL_NOT_FOUND")
        employee = tx.one(
            "SELECT id, email, is_active FROM employees WHERE id = %s FOR UPDATE",
            (reference["employee_id"],),
        )
        credential = tx.one(
            "SELECT token_hash, revoked_at, expires_at FROM qr_credentials "
            "WHERE id = %s FOR UPDATE",
            (reference["qr_id"],),
        )
        queued = tx.one(
            "SELECT id, recipient_email, payload_ciphertext, status, last_error "
            "FROM email_queue WHERE id = %s FOR UPDATE",
            (email_id,),
        )
        return employee, credential, queued

    def validated_preview(self, tx, employee, credential, queued, *, statuses=("QUEUED",), include_svg=True):
        if (
            not queued
            or not employee
            or not credential
            or not employee["is_active"]
            or employee["email"] != queued["recipient_email"]
            or queued["status"] not in statuses
            or queued["payload_ciphertext"] is None
            or credential["revoked_at"] is not None
            or (credential["expires_at"] is not None and credential["expires_at"] <= tx.now())
        ):
            raise DomainError("EMAIL_NOT_DELIVERABLE")
        try:
            payload = json.loads(self.vault.decrypt(queued["payload_ciphertext"]))
            if (
                type(payload["employee_id"]) is not int
                or payload["employee_id"] != employee["id"]
                or not hmac.compare_digest(token_digest(payload["token"]), bytes(credential["token_hash"]))
                or (include_svg and not isinstance(payload["qr_svg"], str))
            ):
                raise DomainError("INVALID_EMAIL_PAYLOAD")
            return EmailPreview(
                email_id=queued["id"],
                recipient_email=queued["recipient_email"],
                employee_name=required_text(payload["employee_name"], "employee_name", 150),
                token=payload["token"],
                qr_svg=payload["qr_svg"] if include_svg else "",
            )
        except (DomainError, KeyError, TypeError, ValueError):
            raise DomainError("INVALID_EMAIL_PAYLOAD") from None

    def preview(self, context, email_id):
        if type(email_id) is not int or email_id < 1:
            raise DomainError("INVALID_EMAIL_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            employee, credential, queued = self.lock_email(tx, email_id)
            result = self.validated_preview(tx, employee, credential, queued)
        return result
