from .delivery import delivery_from_settings
from .email_worker import EmailDeliveryWorker
from .errors import DomainError


_OUTCOMES = frozenset({"SENT", "ALREADY_SENT", "CANCELLED", "FAILED", "NEEDS_REVIEW"})
_SAFE_CODES = frozenset({
    "EMAIL_DELIVERY_NEEDS_REVIEW", "EMAIL_CLAIM_UNCONFIRMED", "EMAIL_CLAIM_LOST",
    "EMAIL_NOT_FOUND", "INVALID_EMAIL_ID", "INVALID_EMAIL_STATUS", "EMAIL_APPROVAL_REQUIRED",
    "INVALID_EMAIL_PAYLOAD", "INVALID_EMAIL_SENDER", "INVALID_EMAIL_RECIPIENT",
    "TEST_EMAIL_RECIPIENT_FORBIDDEN", "INVALID_EMAIL_CONTENT", "INVALID_QR",
    "GMAIL_CONFIGURATION_REQUIRED", "EMAIL_AUTHENTICATION_FAILED", "EMAIL_SENDER_REJECTED",
    "EMAIL_RECIPIENT_REJECTED", "EMAIL_DATA_REJECTED", "EMAIL_DELIVERY_FAILED",
    "EMAIL_NOT_DELIVERABLE", "REAL_EMAIL_NOT_AUTHORIZED",
})


class EmailActions:
    def __init__(self, runtime, services):
        self.runtime = runtime
        self.services = services

    def _enabled(self):
        if self.runtime.email_send_enabled is not True or self.runtime.email_backend not in {"gmail", "ses"}:
            raise DomainError("REAL_EMAIL_NOT_AUTHORIZED")

    def _worker(self):
        delivery = delivery_from_settings(self.runtime)
        return EmailDeliveryWorker(
            self.services.database, self.services.qr.vault, self.services.qr.renderer, delivery,
        )

    def _send(self, worker, email_id):
        try:
            result = worker.send(email_id, allow_real_email=True)
        except DomainError as error:
            if error.code in {"REAL_EMAIL_NOT_AUTHORIZED", "EMAIL_APPROVAL_REQUIRED", "EMAIL_NOT_FOUND", "INVALID_EMAIL_ID"}:
                return {"email_id": email_id, "status": "FAILED", "code": error.code}
            code = error.code if error.code in {"EMAIL_CLAIM_UNCONFIRMED", "EMAIL_CLAIM_LOST"} else "EMAIL_DELIVERY_NEEDS_REVIEW"
            return {"email_id": email_id, "status": "NEEDS_REVIEW", "code": code}
        except Exception:
            return {"email_id": email_id, "status": "NEEDS_REVIEW", "code": "EMAIL_DELIVERY_NEEDS_REVIEW"}
        if not isinstance(result, dict) or result.get("email_id") != email_id or result.get("status") not in _OUTCOMES:
            return {"email_id": email_id, "status": "NEEDS_REVIEW", "code": "EMAIL_DELIVERY_NEEDS_REVIEW"}
        code = result.get("code")
        return {
            "email_id": email_id, "status": result["status"],
            "code": code if code is None or isinstance(code, str) and code in _SAFE_CODES else "EMAIL_DELIVERY_FAILED",
        }

    def send_single(self, context, employee_id, email_id):
        self._enabled()
        approved = self.services.email_queue.approve_single(context, email_id, employee_id)
        if type(approved) is not int or approved != email_id:
            raise DomainError("EMAIL_APPROVAL_UNCONFIRMED")
        return self._send(self._worker(), email_id)

    def approve_bulk(self, context, email_ids):
        self._enabled()
        return self.services.email_queue.approve_bulk(context, email_ids)

    @property
    def process_batch_size(self):
        limit = getattr(self.runtime, "email_process_limit", 10)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise DomainError("INVALID_SETTING_EMAIL_PROCESS_LIMIT")
        return limit

    def process_bulk(self, context, email_ids):
        self._enabled()
        limit = self.process_batch_size
        if (
            not isinstance(email_ids, (list, tuple)) or not 1 <= len(email_ids) <= limit
            or any(type(identifier) is not int or not 1 <= identifier <= 2**64 - 1 for identifier in email_ids)
            or len(set(email_ids)) != len(email_ids)
        ):
            raise DomainError("INVALID_EMAIL_IDS")
        identifiers = list(email_ids)
        approved = self.services.email_queue.validate_approved_bulk(context, identifiers)
        if (
            not isinstance(approved, list) or len(approved) != len(identifiers)
            or any(type(identifier) is not int for identifier in approved)
            or set(approved) != set(identifiers)
        ):
            raise DomainError("EMAIL_APPROVAL_UNCONFIRMED")
        worker = self._worker()
        return {"results": [self._send(worker, identifier) for identifier in identifiers]}
