import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest.mock import Mock

from meal_management.email_dispatcher import EmailDispatcher
from meal_management.email_worker import EmailDeliveryWorker
from meal_management.errors import DomainError
from meal_management.security import generate_token
from test_email_worker import WorkerDatabase


class QueueDatabase:
    def __init__(self, statuses=None):
        self.statuses = dict(statuses or {})
        self.modes = {}
        self.approvals = {}
        self.calls = []
        self.transactions = 0
        self.commits = 0
        self.active = False
        self.fail_commit = False
        self.read_failures = 0
        self.after_commit = None
        self.rows_override = None

    def select(self, sql, params):
        self.calls.append((sql, params))
        if self.read_failures:
            self.read_failures -= 1
            raise RuntimeError("private database connection details")
        if self.rows_override is not None:
            return self.rows_override
        status, bulk, legacy, limit = params
        return [
            {"id": identifier} for identifier, stored in sorted(self.statuses.items())
            if stored == status and self.modes.get(identifier, "BULK") in {bulk, legacy}
            and self.approvals.get(identifier, True)
        ][:limit]

    @contextmanager
    def transaction(self):
        self.transactions += 1
        self.active = True
        try:
            yield Mock(all=self.select)
            if self.fail_commit:
                raise RuntimeError("private database commit details")
            self.commits += 1
        finally:
            self.active = False
        if self.after_commit:
            self.after_commit()


class PollStop:
    def __init__(self, waits=1, after_wait=None):
        self.limit = waits
        self.delays = []
        self.stopped = False
        self.after_wait = after_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.delays.append(seconds)
        if self.after_wait:
            self.after_wait(self)
        if len(self.delays) >= self.limit:
            self.stopped = True
        return self.stopped


class SelectedWorkerDatabase(WorkerDatabase):
    @contextmanager
    def transaction(self):
        with super().transaction() as tx:
            tx.all = lambda sql, params: [{"id": self.queue["id"]}] if (
                self.queue["status"] == "QUEUED" and self.queue["delivery_mode"] in {"BULK", "LEGACY"}
                and self.queue["approved_by_staff_id"] is not None and self.queue["approved_at"] is not None
            ) else []
            yield tx


class EmailDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.db = QueueDatabase({1: "QUEUED", 2: "QUEUED"})
        self.worker = Mock(delivery=Mock(enabled=True))
        self.worker.send.side_effect = self.accepted
        self.dispatcher = EmailDispatcher(self.db, self.worker)

    def accepted(self, identifier, **kwargs):
        self.db.statuses[identifier] = "SENT"
        return {"email_id": identifier, "status": "SENT", "code": None}

    def test_poll_and_batch_configuration_are_bounded(self):
        for interval in (True, 0, -1, "5", float("inf"), float("nan"), 301):
            with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_POLL_INTERVAL"):
                EmailDispatcher(self.db, self.worker, interval_seconds=interval)
        for size in (True, 0, -1, 1.5, "10", 101):
            with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_BATCH_SIZE"):
                EmailDispatcher(self.db, self.worker, batch_size=size)
        self.assertEqual(self.db.transactions, 0)

    def test_disabled_delivery_stops_before_any_database_access(self):
        self.worker.delivery.enabled = False
        self.assertEqual(self.dispatcher.dispatch_once()["status"], "DISABLED")
        self.dispatcher.run(PollStop())
        self.assertEqual(self.db.transactions, 0)
        self.worker.send.assert_not_called()
        self.assertFalse(self.dispatcher.snapshot()["running"])

    def test_selection_commits_before_any_worker_send_and_is_parameterized(self):
        def accepted(identifier, **kwargs):
            self.assertFalse(self.db.active)
            self.assertEqual(self.db.commits, 1)
            self.assertEqual(kwargs, {"allow_real_email": True})
            return self.accepted(identifier, **kwargs)
        self.worker.send.side_effect = accepted
        result = self.dispatcher.dispatch_once()
        self.assertEqual(result["sent"], 2)
        sql, params = self.db.calls[0]
        self.assertEqual(params, ("QUEUED", "BULK", "LEGACY", 10))
        self.assertEqual(sql, "SELECT id FROM email_queue WHERE status = %s "
                         "AND delivery_mode IN (%s, %s) "
                         "AND approved_by_staff_id IS NOT NULL AND approved_at IS NOT NULL "
                         "ORDER BY created_at, id LIMIT %s")
        self.assertNotIn("FOR UPDATE", sql)

    def test_failed_selection_commit_cannot_start_any_delivery(self):
        self.db.fail_commit = True
        result = self.dispatcher.dispatch_once()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["last_error"], "EMAIL_QUEUE_UNAVAILABLE")
        self.worker.send.assert_not_called()

    def test_only_queued_rows_are_selected_and_batch_size_is_respected(self):
        self.db.statuses = {1: "FAILED", 2: "SENT", 3: "CANCELLED", 4: "QUEUED", 5: "QUEUED"}
        dispatcher = EmailDispatcher(self.db, self.worker, batch_size=1)
        self.assertEqual(dispatcher.dispatch_once()["selected"], 1)
        self.worker.send.assert_called_once_with(4, allow_real_email=True)
        self.assertEqual(self.db.statuses[5], "QUEUED")

    def test_only_approved_bulk_or_legacy_rows_are_selected(self):
        self.db.statuses = {
            1: "DRAFT", 2: "PENDING_APPROVAL", 3: "QUEUED", 4: "QUEUED", 5: "QUEUED", 6: "QUEUED",
        }
        self.db.modes = {1: "SINGLE", 2: "BULK", 3: "SINGLE", 4: "BULK", 5: "BULK", 6: "LEGACY"}
        self.db.approvals = {1: False, 2: False, 3: True, 4: False, 5: True, 6: True}
        result = self.dispatcher.dispatch_once()
        self.assertEqual(result["sent"], 2)
        self.assertEqual([call.args[0] for call in self.worker.send.call_args_list], [5, 6])
        self.assertEqual(self.db.statuses[3], "QUEUED")
        self.assertEqual(self.db.statuses[4], "QUEUED")

    def test_new_single_and_bulk_registration_never_start_provider_delivery(self):
        self.db.statuses = {1: "DRAFT", 2: "PENDING_APPROVAL"}
        self.assertEqual(self.dispatcher.dispatch_once()["selected"], 0)
        self.worker.send.assert_not_called()

    def test_same_selected_row_claimed_elsewhere_reuses_worker_result(self):
        self.worker.send.side_effect = lambda identifier, **kwargs: {"email_id": identifier, "status": "ALREADY_SENT", "code": None}
        result = self.dispatcher.dispatch_once()
        self.assertEqual(result["already_sent"], 2)
        self.assertEqual(result["sent"], 0)

    def test_failed_and_review_outcomes_are_not_automatically_retried(self):
        def failed(identifier, **kwargs):
            self.db.statuses[identifier] = "FAILED"
            status = "FAILED" if identifier == 1 else "NEEDS_REVIEW"
            code = "EMAIL_DELIVERY_FAILED" if identifier == 1 else "EMAIL_DELIVERY_NEEDS_REVIEW"
            return {"email_id": identifier, "status": status, "code": code}
        self.worker.send.side_effect = failed
        result = self.dispatcher.dispatch_once()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.dispatcher.dispatch_once()["needs_review"], 1)
        self.assertEqual(self.dispatcher.dispatch_once()["selected"], 0)
        self.assertEqual(self.worker.send.call_count, 2)

    def test_global_sender_failures_halt_loop_and_preserve_remaining_queue(self):
        for code in ("EMAIL_AUTHENTICATION_FAILED", "GMAIL_CONFIGURATION_REQUIRED", "INVALID_EMAIL_SENDER", "EMAIL_SENDER_REJECTED", "REAL_EMAIL_NOT_AUTHORIZED"):
            self.setUp()
            def failed(identifier, **kwargs):
                self.db.statuses[identifier] = "FAILED"
                return {"email_id": identifier, "status": "FAILED", "code": code}
            self.worker.send.side_effect = failed
            stop = PollStop(waits=2)
            self.dispatcher.run(stop)
            self.worker.send.assert_called_once_with(1, allow_real_email=True)
            self.assertEqual(self.db.statuses[2], "QUEUED")
            self.assertEqual(stop.delays, [])
            self.assertEqual(self.dispatcher.snapshot()["status"], "HALTED")
            self.assertEqual(self.dispatcher.snapshot()["last_error"], code)
            self.assertFalse(self.dispatcher.snapshot()["running"])

    def test_invalid_recipient_or_qr_affects_only_that_email(self):
        for code in ("INVALID_QR", "INVALID_EMAIL_PAYLOAD", "INVALID_EMAIL_RECIPIENT", "TEST_EMAIL_RECIPIENT_FORBIDDEN", "EMAIL_RECIPIENT_REJECTED"):
            self.setUp()
            def outcome(identifier, **kwargs):
                if identifier == 1:
                    self.db.statuses[identifier] = "FAILED"
                    return {"email_id": identifier, "status": "FAILED", "code": code}
                return self.accepted(identifier, **kwargs)
            self.worker.send.side_effect = outcome
            result = self.dispatcher.dispatch_once()
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["sent"], 1)

    def test_provider_outage_backs_off_before_next_row_without_retrying_failed_row(self):
        def failed(identifier, **kwargs):
            self.db.statuses[identifier] = "FAILED"
            return {"email_id": identifier, "status": "FAILED", "code": "EMAIL_DELIVERY_FAILED"}
        self.worker.send.side_effect = failed
        stop = PollStop(waits=2)
        self.dispatcher.run(stop)
        self.assertEqual(stop.delays, [5, 10])
        self.assertEqual([call.args[0] for call in self.worker.send.call_args_list], [1, 2])

    def test_worker_exception_stops_batch_and_exposes_only_fixed_code(self):
        for failure in (RuntimeError("private recipient/password"), DomainError("private provider diagnostics")):
            self.setUp()
            self.worker.send.side_effect = failure
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = self.dispatcher.dispatch_once()
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["last_error"], "EMAIL_DISPATCH_FAILED")
            self.assertEqual(result["processed"], 1)
            self.worker.send.assert_called_once()
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("private", repr(self.dispatcher.snapshot()))

    def test_known_worker_failure_code_is_preserved_without_exception_text(self):
        self.worker.send.side_effect = DomainError("EMAIL_CLAIM_UNCONFIRMED")
        self.assertEqual(self.dispatcher.dispatch_once()["last_error"], "EMAIL_CLAIM_UNCONFIRMED")

    def test_provider_result_details_are_not_copied_to_snapshot(self):
        self.worker.send.side_effect = lambda identifier, **kwargs: {
            "email_id": identifier, "status": "FAILED", "code": "private provider text", "recipient": "private-address",
        }
        self.assertEqual(self.dispatcher.dispatch_once()["last_error"], "EMAIL_DELIVERY_FAILED")
        snapshot = self.dispatcher.snapshot()
        self.assertNotIn("private", repr(snapshot))
        self.assertNotIn("email_id", snapshot)

    def test_malformed_selection_or_worker_results_cannot_be_treated_as_success(self):
        for rows in ([{"id": True}], [{"id": 0}], [{"id": "1"}], [{"id": 1}, {"id": 1}], [{}]):
            self.setUp()
            self.db.rows_override = rows
            self.assertEqual(self.dispatcher.dispatch_once()["status"], "ERROR")
            self.worker.send.assert_not_called()
        for outcome in (None, {}, {"email_id": 99, "status": "SENT"}, {"email_id": 1, "status": "UNKNOWN"}):
            self.setUp()
            self.worker.send.side_effect = None
            self.worker.send.return_value = outcome
            self.assertEqual(self.dispatcher.dispatch_once()["status"], "ERROR")

    def test_preexisting_stop_prevents_selection_and_delivery(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.dispatcher.dispatch_once(stop)["status"], "STOPPED")
        self.dispatcher.run(stop)
        self.assertEqual(self.db.transactions, 0)
        self.worker.send.assert_not_called()

    def test_stop_after_selection_commit_prevents_first_send(self):
        stop = threading.Event()
        self.db.after_commit = stop.set
        result = self.dispatcher.dispatch_once(stop)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["status"], "STOPPED")
        self.worker.send.assert_not_called()

    def test_stop_during_one_send_finishes_it_without_starting_next_email(self):
        stop = threading.Event()
        def accepted(identifier, **kwargs):
            stop.set()
            return self.accepted(identifier, **kwargs)
        self.worker.send.side_effect = accepted
        result = self.dispatcher.dispatch_once(stop)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(self.db.statuses[2], "QUEUED")

    def test_delivery_becoming_disabled_between_ids_stops_the_batch(self):
        def accepted(identifier, **kwargs):
            self.worker.delivery.enabled = False
            return self.accepted(identifier, **kwargs)
        self.worker.send.side_effect = accepted
        self.assertEqual(self.dispatcher.dispatch_once()["status"], "DISABLED")
        self.worker.send.assert_called_once()

    def test_idle_polling_uses_interruptible_wait_and_does_not_busy_loop(self):
        self.db.statuses = {}
        stop = PollStop(waits=3)
        self.dispatcher.run(stop)
        self.assertEqual(stop.delays, [5, 5, 5])
        self.assertEqual(self.db.transactions, 3)
        self.assertFalse(self.dispatcher.snapshot()["running"])
        self.assertEqual(self.dispatcher.snapshot()["status"], "STOPPED")

    def test_database_outage_backoff_is_bounded_and_resets_after_recovery(self):
        self.db.statuses = {}
        self.db.read_failures = 7
        stop = PollStop(waits=8)
        self.dispatcher.run(stop)
        self.assertEqual(stop.delays, [5, 10, 20, 40, 60, 60, 60, 5])
        self.assertIsNone(self.dispatcher.snapshot()["last_error"])

    def test_unexpected_poll_exception_is_sanitized_without_killing_loop(self):
        self.dispatcher.dispatch_once = Mock(side_effect=RuntimeError("private polling details"))
        stop = PollStop(waits=2)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.dispatcher.run(stop)
        self.assertEqual(stop.delays, [5, 10])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.dispatcher.snapshot()["last_error"], "EMAIL_DISPATCH_FAILED")
        self.assertFalse(self.dispatcher.snapshot()["running"])

    def test_restart_polls_only_remaining_queued_rows(self):
        dispatcher = EmailDispatcher(self.db, self.worker, batch_size=1)
        dispatcher.run(PollStop(waits=1))
        restarted = EmailDispatcher(self.db, self.worker, batch_size=1)
        restarted.run(PollStop(waits=1))
        self.assertEqual([call.args[0] for call in self.worker.send.call_args_list], [1, 2])
        self.assertEqual(restarted.snapshot()["sent"], 1)

    def test_snapshot_is_a_copy_with_safe_counters_and_utc_timestamp(self):
        self.dispatcher.dispatch_once()
        snapshot = self.dispatcher.snapshot()
        self.assertEqual(snapshot["sent"], 2)
        self.assertTrue(snapshot["last_poll_at"].endswith("+00:00"))
        snapshot["sent"] = 999
        self.assertEqual(self.dispatcher.snapshot()["sent"], 2)

    def test_two_dispatchers_reuse_real_worker_durable_claim_without_second_delivery(self):
        token = generate_token()
        db = SelectedWorkerDatabase(token)
        db.queue["delivery_mode"] = "BULK"
        payload = {"employee_id": 7, "employee_name": "Fictional Employee", "token": token}
        vault = Mock(decrypt=Mock(return_value=json.dumps(payload)))
        delivery = Mock(enabled=True, send=Mock(return_value="<fake-accepted>"))
        worker = EmailDeliveryWorker(db, vault, Mock(), delivery)
        dispatchers = [EmailDispatcher(db, worker) for _ in range(2)]
        barrier = threading.Barrier(2)
        def poll(dispatcher):
            barrier.wait(timeout=2)
            return dispatcher.dispatch_once()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(poll, dispatchers))
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.assertEqual(db.queue["status"], "SENT")
        delivery.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
