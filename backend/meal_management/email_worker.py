import hmac
import secrets

from .delivery import DeliveryError, validate_email_recipient
from .email_queue import EmailQueueService
from .errors import DomainError


_CLAIM_PREFIX = "EMAIL_DELIVERY_CLAIMED_"
_REVIEW = "EMAIL_DELIVERY_NEEDS_REVIEW"
_SAFE_FAILURES = frozenset({
    "INVALID_EMAIL_PAYLOAD", "INVALID_EMAIL_SENDER", "INVALID_EMAIL_RECIPIENT",
    "TEST_EMAIL_RECIPIENT_FORBIDDEN", "INVALID_EMAIL_CONTENT", "INVALID_QR",
    "GMAIL_CONFIGURATION_REQUIRED", "EMAIL_AUTHENTICATION_FAILED",
    "EMAIL_SENDER_REJECTED", "EMAIL_RECIPIENT_REJECTED", "EMAIL_DATA_REJECTED",
    "EMAIL_DELIVERY_FAILED", "EMAIL_NOT_DELIVERABLE", "REAL_EMAIL_NOT_AUTHORIZED",
})


class EmailDeliveryWorker:
    def __init__(self, db, vault, renderer, delivery):
        self.db = db
        self.queue = EmailQueueService(db, vault, renderer)
        self.delivery = delivery

    def _result(self, email_id, status, code=None):
        return {"email_id": email_id, "status": status, "code": code}

    def _existing(self, email_id, queued):
        if queued is None:
            raise DomainError("EMAIL_NOT_FOUND")
        status = queued["status"]
        if status == "SENT":
            return self._result(email_id, "ALREADY_SENT")
        if status == "CANCELLED":
            code = queued.get("last_error")
            return self._result(email_id, "CANCELLED", code if code in _SAFE_FAILURES else "EMAIL_NOT_DELIVERABLE")
        if status == "FAILED":
            code = queued.get("last_error")
            if isinstance(code, str) and (code.startswith(_CLAIM_PREFIX) or code == _REVIEW):
                return self._result(email_id, "NEEDS_REVIEW", _REVIEW)
            return self._result(email_id, "FAILED", code if code in _SAFE_FAILURES else "EMAIL_DELIVERY_FAILED")
        if status != "QUEUED":
            raise DomainError("INVALID_EMAIL_STATUS")
        return None

    def _set_status(self, tx, email_id, status, code, expected_status, expected_error):
        changed = tx.execute(
            "UPDATE email_queue SET status = %s, last_error = %s, updated_at = %s, "
            "payload_ciphertext = CASE WHEN %s IN ('SENT', 'CANCELLED') "
            "THEN NULL ELSE payload_ciphertext END "
            "WHERE id = %s AND status = %s AND last_error <=> %s",
            (status, code, tx.now(), status, email_id, expected_status, expected_error),
        )
        if changed != 1:
            raise DomainError("EMAIL_CLAIM_LOST")

    def _validate(self, tx, employee, credential, queued, statuses):
        try:
            preview = self.queue.validated_preview(
                tx, employee, credential, queued, statuses=statuses, include_svg=False,
            )
            validate_email_recipient(preview.recipient_email)
            return preview, None
        except DomainError as error:
            code = error.code if error.code in _SAFE_FAILURES else "INVALID_EMAIL_PAYLOAD"
            status = "CANCELLED" if code in {
                "EMAIL_NOT_DELIVERABLE", "INVALID_EMAIL_RECIPIENT", "TEST_EMAIL_RECIPIENT_FORBIDDEN",
            } else "FAILED"
            return None, self._result(queued["id"], status, code)

    def _claim(self, email_id):
        claim = _CLAIM_PREFIX + secrets.token_hex(16)
        with self.db.transaction() as tx:
            employee, credential, queued = self.queue.lock_email(tx, email_id)
            result = self._existing(email_id, queued)
            if result is None:
                _, result = self._validate(tx, employee, credential, queued, ("QUEUED",))
                if result is None:
                    self._set_status(tx, email_id, "FAILED", claim, "QUEUED", queued.get("last_error"))
                else:
                    self._set_status(tx, email_id, result["status"], result["code"], "QUEUED", queued.get("last_error"))
        return claim, result

    def _deliver(self, email_id, claim):
        with self.db.transaction() as tx:
            employee, credential, queued = self.queue.lock_email(tx, email_id)
            marker = queued.get("last_error") if queued else None
            if (
                not queued or queued["status"] != "FAILED" or not isinstance(marker, str)
                or not hmac.compare_digest(marker, claim)
            ):
                return self._existing(email_id, queued) or self._result(email_id, "NEEDS_REVIEW", _REVIEW)
            preview, result = self._validate(tx, employee, credential, queued, ("FAILED",))
            if result is None:
                try:
                    accepted = self.delivery.send(preview, allow_real_email=True)
                    if not isinstance(accepted, str) or not accepted.strip():
                        result = self._result(email_id, "NEEDS_REVIEW", _REVIEW)
                    else:
                        result = self._result(email_id, "SENT")
                except DeliveryError as error:
                    if error.uncertain:
                        result = self._result(email_id, "NEEDS_REVIEW", _REVIEW)
                    else:
                        code = error.code if error.code in _SAFE_FAILURES else "EMAIL_DELIVERY_FAILED"
                        result = self._result(email_id, "FAILED", code)
                except Exception:
                    result = self._result(email_id, "NEEDS_REVIEW", _REVIEW)
            stored_status = "FAILED" if result["status"] == "NEEDS_REVIEW" else result["status"]
            self._set_status(tx, email_id, stored_status, result["code"], "FAILED", claim)
        return result

    def send(self, email_id, *, allow_real_email=False):
        if allow_real_email is not True or getattr(self.delivery, "enabled", False) is not True:
            raise DomainError("REAL_EMAIL_NOT_AUTHORIZED")
        if type(email_id) is not int or not 1 <= email_id <= 2**64 - 1:
            raise DomainError("INVALID_EMAIL_ID")
        try:
            claim, result = self._claim(email_id)
        except DomainError:
            raise
        except Exception:
            raise DomainError("EMAIL_CLAIM_UNCONFIRMED") from None
        if result is not None:
            return result
        try:
            return self._deliver(email_id, claim)
        except Exception:
            return self._result(email_id, "NEEDS_REVIEW", _REVIEW)
