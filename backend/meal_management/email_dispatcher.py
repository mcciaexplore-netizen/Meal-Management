import math
import threading
from datetime import datetime, timezone

from .errors import DomainError


_OUTCOMES = {
    "SENT": "sent", "ALREADY_SENT": "already_sent", "CANCELLED": "cancelled",
    "FAILED": "failed", "NEEDS_REVIEW": "needs_review",
}
_CODES = frozenset({
    "EMAIL_DELIVERY_NEEDS_REVIEW", "INVALID_EMAIL_PAYLOAD", "INVALID_EMAIL_SENDER",
    "INVALID_EMAIL_RECIPIENT", "TEST_EMAIL_RECIPIENT_FORBIDDEN", "INVALID_EMAIL_CONTENT",
    "INVALID_QR", "GMAIL_CONFIGURATION_REQUIRED", "EMAIL_AUTHENTICATION_FAILED",
    "EMAIL_SENDER_REJECTED", "EMAIL_RECIPIENT_REJECTED", "EMAIL_DATA_REJECTED",
    "EMAIL_DELIVERY_FAILED", "EMAIL_NOT_DELIVERABLE", "REAL_EMAIL_NOT_AUTHORIZED",
    "EMAIL_CLAIM_UNCONFIRMED", "EMAIL_CLAIM_LOST", "EMAIL_NOT_FOUND",
    "INVALID_EMAIL_STATUS", "EMAIL_QUEUE_UNAVAILABLE", "EMAIL_DISPATCH_FAILED",
})
_GLOBAL_FAILURES = frozenset({
    "EMAIL_AUTHENTICATION_FAILED", "GMAIL_CONFIGURATION_REQUIRED", "INVALID_EMAIL_SENDER",
    "EMAIL_SENDER_REJECTED", "REAL_EMAIL_NOT_AUTHORIZED",
})
_PROVIDER_OUTAGES = frozenset({
    "EMAIL_DELIVERY_FAILED", "EMAIL_DELIVERY_NEEDS_REVIEW", "EMAIL_DATA_REJECTED",
})


class EmailDispatcher:
    def __init__(self, db, worker, *, interval_seconds=5, batch_size=10):
        if type(interval_seconds) not in {int, float} or not math.isfinite(interval_seconds) or not 1 <= interval_seconds <= 300:
            raise DomainError("INVALID_EMAIL_POLL_INTERVAL")
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise DomainError("INVALID_EMAIL_BATCH_SIZE")
        self.db = db
        self.worker = worker
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._state_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._state = {
            "running": False, "status": "IDLE", "polls": 0, "last_error": None,
            "last_poll_at": None, "selected": 0, "processed": 0,
            **{counter: 0 for counter in _OUTCOMES.values()},
        }

    def _enabled(self):
        return getattr(getattr(self.worker, "delivery", None), "enabled", False) is True

    def _stopped(self, stop_event):
        return stop_event is not None and stop_event.is_set()

    def _error(self, error):
        code = getattr(error, "code", None)
        return code if isinstance(code, str) and code in _CODES else "EMAIL_DISPATCH_FAILED"

    def _batch(self, status="OK", error=None):
        return {
            "status": status, "last_error": error, "selected": 0, "processed": 0,
            **{counter: 0 for counter in _OUTCOMES.values()},
        }

    def _publish(self, result):
        with self._state_lock:
            self._state["status"] = result["status"]
            self._state["last_error"] = result["last_error"]
            self._state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
            self._state["polls"] += 1
            for counter in ("selected", "processed", *_OUTCOMES.values()):
                self._state[counter] += result[counter]
        return result

    def snapshot(self):
        with self._state_lock:
            result = dict(self._state)
        result["enabled"] = self._enabled()
        return result

    def _select(self):
        with self.db.transaction() as tx:
            rows = tx.all(
                "SELECT id FROM email_queue WHERE status = %s "
                "ORDER BY created_at, id LIMIT %s",
                ("QUEUED", self.batch_size),
            )
            identifiers = [row["id"] for row in rows]
            if (
                len(identifiers) > self.batch_size
                or any(type(identifier) is not int or not 1 <= identifier <= 2**64 - 1 for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
            ):
                raise DomainError("EMAIL_QUEUE_UNAVAILABLE")
        return identifiers

    def dispatch_once(self, stop_event=None):
        with self._dispatch_lock:
            if self._stopped(stop_event):
                return self._publish(self._batch("STOPPED"))
            if not self._enabled():
                return self._publish(self._batch("DISABLED", "REAL_EMAIL_NOT_AUTHORIZED"))
            result = self._batch()
            try:
                identifiers = self._select()
            except Exception:
                return self._publish(self._batch("ERROR", "EMAIL_QUEUE_UNAVAILABLE"))
            result["selected"] = len(identifiers)
            for identifier in identifiers:
                if self._stopped(stop_event):
                    result["status"] = "STOPPED"
                    break
                if not self._enabled():
                    result.update(status="DISABLED", last_error="REAL_EMAIL_NOT_AUTHORIZED")
                    break
                result["processed"] += 1
                try:
                    outcome = self.worker.send(identifier, allow_real_email=True)
                    if not isinstance(outcome, dict) or outcome.get("email_id") != identifier or outcome.get("status") not in _OUTCOMES:
                        raise DomainError("EMAIL_DISPATCH_FAILED")
                    result[_OUTCOMES[outcome["status"]]] += 1
                    code = outcome.get("code")
                    if code is not None:
                        result["last_error"] = code if isinstance(code, str) and code in _CODES else "EMAIL_DELIVERY_FAILED"
                        if result["last_error"] in _GLOBAL_FAILURES:
                            result["status"] = "HALTED"
                            break
                        if result["last_error"] in _PROVIDER_OUTAGES:
                            result["status"] = "ERROR"
                            break
                except Exception as error:
                    result.update(status="ERROR", last_error=self._error(error))
                    if result["last_error"] in _GLOBAL_FAILURES:
                        result["status"] = "HALTED"
                    break
            return self._publish(result)

    def run(self, stop_event):
        if not self._run_lock.acquire(blocking=False):
            return
        with self._state_lock:
            self._state["running"] = True
        failures = 0
        try:
            while not self._stopped(stop_event):
                try:
                    result = self.dispatch_once(stop_event)
                except Exception as error:
                    result = self._publish(self._batch("ERROR", self._error(error)))
                if result["status"] in {"STOPPED", "DISABLED", "HALTED"}:
                    break
                failures = min(failures + 1, 8) if result["status"] == "ERROR" else 0
                delay = min(self.interval_seconds * (2 ** max(0, failures - 1)), max(60, self.interval_seconds))
                if stop_event.wait(delay):
                    break
        except Exception as error:
            with self._state_lock:
                self._state.update(status="ERROR", last_error=self._error(error))
        finally:
            with self._state_lock:
                self._state["running"] = False
                if self._state["status"] not in {"DISABLED", "ERROR", "HALTED"}:
                    self._state["status"] = "STOPPED"
            self._run_lock.release()
