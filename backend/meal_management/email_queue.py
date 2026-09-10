import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .auth import audit, require_actor
from .errors import DomainError
from .security import required_text, token_digest


_FAILURE_CODES = frozenset({
    "INVALID_EMAIL_PAYLOAD", "INVALID_EMAIL_SENDER", "INVALID_EMAIL_RECIPIENT",
    "TEST_EMAIL_RECIPIENT_FORBIDDEN", "INVALID_EMAIL_CONTENT", "INVALID_QR",
    "GMAIL_CONFIGURATION_REQUIRED", "EMAIL_AUTHENTICATION_FAILED", "EMAIL_SENDER_REJECTED",
    "EMAIL_RECIPIENT_REJECTED", "EMAIL_DATA_REJECTED", "EMAIL_DELIVERY_FAILED",
    "EMAIL_NOT_DELIVERABLE", "REAL_EMAIL_NOT_AUTHORIZED", "EMAIL_APPROVAL_REQUIRED",
})


def email_delivery_state(row):
    status = row["status"]
    code = row.get("last_error")
    if status == "FAILED":
        if not isinstance(code, str) or code not in _FAILURE_CODES:
            return "NEEDS_REVIEW", "EMAIL_DELIVERY_NEEDS_REVIEW"
        return status, code
    if code is None:
        return status, None
    return status, code if isinstance(code, str) and code in _FAILURE_CODES else "EMAIL_PROCESSING_FAILED"


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

    def enqueue_employee(self, tx, qr_id, employee, token, *, bulk_batch_id=None):
        if bulk_batch_id is not None and (type(bulk_batch_id) is not int or not 1 <= bulk_batch_id <= 2**64 - 1):
            raise DomainError("INVALID_EMAIL_BATCH_ID")
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
            "(qr_id, recipient_email, payload_ciphertext, status, created_at, updated_at, delivery_mode, bulk_batch_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                qr_id,
                employee["email"],
                self.vault.encrypt(payload),
                "PENDING_APPROVAL" if bulk_batch_id is not None else "DRAFT",
                now,
                now,
                "BULK" if bulk_batch_id is not None else "SINGLE",
                bulk_batch_id,
            ),
        )

    def cancel_for_qr(self, tx, qr_id):
        tx.execute(
            "UPDATE email_queue SET "
            "status = CASE WHEN status IN ('DRAFT', 'PENDING_APPROVAL', 'QUEUED', 'FAILED') "
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
                "AND q.delivery_mode IN ('BULK', 'LEGACY') "
                "AND q.approved_by_staff_id IS NOT NULL AND q.approved_at IS NOT NULL "
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
            "SELECT id, recipient_email, payload_ciphertext, status, last_error, "
            "delivery_mode, bulk_batch_id, approved_by_staff_id, approved_at "
            "FROM email_queue WHERE id = %s FOR UPDATE",
            (email_id,),
        )
        return employee, credential, queued

    def validated_preview(self, tx, employee, credential, queued, *, statuses=("DRAFT", "PENDING_APPROVAL", "QUEUED"), include_svg=True):
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

    def _identifiers(self, email_ids, maximum=100):
        if (
            not isinstance(email_ids, list) or not 1 <= len(email_ids) <= maximum
            or any(type(identifier) is not int or not 1 <= identifier <= 2**64 - 1 for identifier in email_ids)
            or len(set(email_ids)) != len(email_ids)
        ):
            raise DomainError("INVALID_EMAIL_IDS")
        return list(email_ids)

    def _approved(self, queued):
        return (
            type(queued.get("approved_by_staff_id")) is int and queued["approved_by_staff_id"] > 0
            and isinstance(queued.get("approved_at"), datetime)
        )

    def _bulk_rows(self, tx, identifiers):
        placeholders = ", ".join(["%s"] * len(identifiers))
        references = tx.all(
            "SELECT q.id FROM email_queue q JOIN qr_credentials c ON c.id = q.qr_id "
            "WHERE q.id IN (" + placeholders + ") ORDER BY c.employee_id, c.id, q.id",
            identifiers,
        )
        if {row["id"] for row in references} != set(identifiers):
            raise DomainError("EMAIL_NOT_FOUND")
        rows = []
        for reference in references:
            employee, credential, queued = self.lock_email(tx, reference["id"])
            if queued is None:
                raise DomainError("EMAIL_NOT_FOUND")
            if queued["delivery_mode"] not in {"BULK", "LEGACY"}:
                raise DomainError("BULK_EMAIL_REQUIRED")
            rows.append((employee, credential, queued))
        return rows

    def _approve(self, tx, actor, queued):
        now = tx.now()
        changed = tx.execute(
            "UPDATE email_queue SET status = 'QUEUED', approved_by_staff_id = %s, approved_at = %s, "
            "updated_at = %s, last_error = NULL WHERE id = %s AND status = %s "
            "AND approved_by_staff_id IS NULL AND approved_at IS NULL",
            (actor.staff_id, now, now, queued["id"], queued["status"]),
        )
        if changed != 1:
            raise DomainError("EMAIL_APPROVAL_CONFLICT")
        audit(tx, actor.staff_id, "EMAIL_DELIVERY_APPROVED", "email_queue", queued["id"], after={"delivery_mode": queued["delivery_mode"]})

    def approve_single(self, context, email_id, employee_id):
        self._identifiers([email_id], 1)
        if type(employee_id) is not int or not 1 <= employee_id <= 2**64 - 1:
            raise DomainError("INVALID_EMPLOYEE_ID")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            employee, credential, queued = self.lock_email(tx, email_id)
            if not employee or employee["id"] != employee_id or queued is None:
                raise DomainError("EMAIL_NOT_FOUND")
            if queued["delivery_mode"] != "SINGLE":
                raise DomainError("SINGLE_EMAIL_REQUIRED")
            if queued["status"] == "DRAFT":
                self.validated_preview(tx, employee, credential, queued, statuses=("DRAFT",), include_svg=False)
                self._approve(tx, actor, queued)
            elif queued["status"] == "QUEUED" and not self._approved(queued):
                raise DomainError("EMAIL_APPROVAL_REQUIRED")
            elif queued["status"] not in {"QUEUED", "SENT", "FAILED", "CANCELLED"}:
                raise DomainError("EMAIL_APPROVAL_REQUIRED")
        return email_id

    def approve_bulk(self, context, email_ids):
        identifiers = self._identifiers(email_ids)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            rows = self._bulk_rows(tx, identifiers)
            pending = []
            for employee, credential, queued in rows:
                if queued["status"] == "PENDING_APPROVAL":
                    self.validated_preview(tx, employee, credential, queued, statuses=("PENDING_APPROVAL",), include_svg=False)
                    pending.append(queued)
                elif queued["status"] == "QUEUED":
                    if not self._approved(queued):
                        raise DomainError("EMAIL_APPROVAL_REQUIRED")
                    self.validated_preview(tx, employee, credential, queued, statuses=("QUEUED",), include_svg=False)
                elif queued["status"] not in {"SENT", "FAILED", "CANCELLED"} or not self._approved(queued):
                    raise DomainError("EMAIL_APPROVAL_REQUIRED")
            for queued in pending:
                self._approve(tx, actor, queued)
            result = {"email_ids": identifiers, "approved_count": len(pending)}
        return result

    def validate_approved_bulk(self, context, email_ids):
        identifiers = self._identifiers(email_ids, 10)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            for _, _, queued in self._bulk_rows(tx, identifiers):
                if not self._approved(queued) or queued["status"] not in {"QUEUED", "SENT", "FAILED", "CANCELLED"}:
                    raise DomainError("EMAIL_APPROVAL_REQUIRED")
        return identifiers

    def status(self, context, email_id, employee_id=None):
        self._identifiers([email_id], 1)
        if employee_id is not None and (type(employee_id) is not int or not 1 <= employee_id <= 2**64 - 1):
            raise DomainError("INVALID_EMPLOYEE_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT q.id, c.employee_id, q.status, q.delivery_mode, q.bulk_batch_id, "
                "q.approved_at, q.last_error FROM email_queue q JOIN qr_credentials c ON c.id = q.qr_id "
                "WHERE q.id = %s",
                (email_id,),
            )
            if row is None or (employee_id is not None and row["employee_id"] != employee_id):
                raise DomainError("EMAIL_NOT_FOUND")
        status, code = email_delivery_state(row)
        approved_at = row["approved_at"]
        if approved_at is not None:
            approved_at = approved_at.replace(tzinfo=timezone.utc) if approved_at.tzinfo is None else approved_at.astimezone(timezone.utc)
        return {
            "id": row["id"], "email_id": row["id"], "status": status,
            "delivery_mode": row["delivery_mode"], "bulk_batch_id": row["bulk_batch_id"],
            "approved_at": approved_at, "code": code,
        }
