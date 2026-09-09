import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class DeliveryPhaseDatabase:
    def __init__(self, database, fail_phase=None, after_claim=None):
        self.database = database
        self.fail_phase = fail_phase
        self.after_claim = after_claim
        self.phase = 0

    @contextmanager
    def transaction(self):
        self.phase += 1
        phase = self.phase
        with self.database.transaction() as tx:
            yield tx
            if phase == self.fail_phase:
                raise RuntimeError("Injected transaction failure")
        if phase == 1 and self.after_claim:
            self.after_claim()


class EmailDeliveryMySQLTests(MealServicesFixture):
    def queued_email(self):
        suffix = uuid4().hex
        return self.employees.register(
            self.admin_context, "MAIL-" + suffix[:20], "Fictional Email Employee",
            "meal-delivery-test-" + suffix + "@gmail.com", self.department_id,
        )

    def worker(self, adapter, database=None):
        from meal_management.email_worker import EmailDeliveryWorker
        from meal_management.security import QrRenderer

        return EmailDeliveryWorker(database or self.db, self.vault, QrRenderer(), adapter)

    def adapter(self):
        return Mock(enabled=True, send=Mock(return_value="<fictional-message-id>"))

    def test_provider_observes_committed_claim_and_sent_replay_does_not_send(self):
        registration = self.queued_email()
        adapter = self.adapter()
        def accepted(preview, **kwargs):
            with self.db.transaction() as tx:
                row = tx.one("SELECT status, last_error FROM email_queue WHERE id = %s", (registration.email_id,))
            self.assertEqual(row["status"], "FAILED")
            self.assertTrue(row["last_error"].startswith("EMAIL_DELIVERY_CLAIMED_"))
            self.assertEqual(preview.qr_svg, "")
            self.assertEqual(kwargs, {"allow_real_email": True})
            return "<fictional-accepted>"
        adapter.send.side_effect = accepted
        worker = self.worker(adapter)
        self.assertEqual(worker.send(registration.email_id, allow_real_email=True)["status"], "SENT")
        row = self.rows("SELECT status, last_error, payload_ciphertext FROM email_queue WHERE id = %s", (registration.email_id,))[0]
        self.assertEqual(row, {"status": "SENT", "last_error": None, "payload_ciphertext": None})
        self.assertEqual(worker.send(registration.email_id, allow_real_email=True)["status"], "ALREADY_SENT")
        adapter.send.assert_called_once()

    def test_simultaneous_workers_claim_and_send_only_once(self):
        registration = self.queued_email()
        adapter = self.adapter()
        barrier = threading.Barrier(2)
        def send():
            barrier.wait(timeout=10)
            return self.worker(adapter).send(registration.email_id, allow_real_email=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(send) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(result["status"] == "SENT" for result in results), 1)
        self.assertTrue(all(result["status"] in {"SENT", "ALREADY_SENT", "NEEDS_REVIEW"} for result in results))
        adapter.send.assert_called_once()

    def test_failed_claim_transaction_never_calls_provider(self):
        from meal_management.errors import DomainError

        registration = self.queued_email()
        adapter = self.adapter()
        database = DeliveryPhaseDatabase(self.db, fail_phase=1)
        with self.assertRaisesRegex(DomainError, "EMAIL_CLAIM_UNCONFIRMED"):
            self.worker(adapter, database).send(registration.email_id, allow_real_email=True)
        adapter.send.assert_not_called()
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (registration.email_id,)), "QUEUED")

    def test_failed_sent_acknowledgement_leaves_durable_claim_and_blocks_resend(self):
        registration = self.queued_email()
        adapter = self.adapter()
        database = DeliveryPhaseDatabase(self.db, fail_phase=2)
        self.assertEqual(self.worker(adapter, database).send(registration.email_id, allow_real_email=True)["status"], "NEEDS_REVIEW")
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (registration.email_id,)), "FAILED")
        self.assertEqual(self.worker(adapter).send(registration.email_id, allow_real_email=True)["status"], "NEEDS_REVIEW")
        adapter.send.assert_called_once()

    def test_employee_deactivation_between_claim_and_send_cancels_delivery(self):
        registration = self.queued_email()
        adapter = self.adapter()
        database = DeliveryPhaseDatabase(
            self.db, after_claim=lambda: self.employees.set_active(self.admin_context, registration.employee_id, False),
        )
        self.assertEqual(self.worker(adapter, database).send(registration.email_id, allow_real_email=True)["status"], "CANCELLED")
        adapter.send.assert_not_called()

    def test_qr_replacement_between_claim_and_send_cancels_old_delivery(self):
        registration = self.queued_email()
        adapter = self.adapter()
        database = DeliveryPhaseDatabase(
            self.db, after_claim=lambda: self.qr.replace_employee(self.admin_context, registration.employee_id),
        )
        self.assertEqual(self.worker(adapter, database).send(registration.email_id, allow_real_email=True)["status"], "CANCELLED")
        adapter.send.assert_not_called()

    def test_reserved_seed_recipient_is_never_passed_to_delivery(self):
        registration, _ = self.employee()
        adapter = self.adapter()
        result = self.worker(adapter).send(registration.email_id, allow_real_email=True)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["code"], "TEST_EMAIL_RECIPIENT_FORBIDDEN")
        adapter.send.assert_not_called()

    def test_uncertain_provider_outcome_stays_failed_without_automatic_retry(self):
        from meal_management.delivery import DeliveryError

        registration = self.queued_email()
        adapter = self.adapter()
        adapter.send.side_effect = DeliveryError("EMAIL_DELIVERY_UNCONFIRMED", uncertain=True)
        worker = self.worker(adapter)
        for _ in range(2):
            self.assertEqual(worker.send(registration.email_id, allow_real_email=True)["status"], "NEEDS_REVIEW")
        self.assertEqual(self.scalar("SELECT last_error FROM email_queue WHERE id = %s", (registration.email_id,)), "EMAIL_DELIVERY_NEEDS_REVIEW")
        adapter.send.assert_called_once()

    def test_payload_with_wrong_qr_digest_fails_without_delivery(self):
        from meal_management.security import generate_token

        registration = self.queued_email()
        row = self.rows("SELECT payload_ciphertext FROM email_queue WHERE id = %s", (registration.email_id,))[0]
        payload = json.loads(self.vault.decrypt(row["payload_ciphertext"]))
        payload["token"] = generate_token()
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE email_queue SET payload_ciphertext = %s WHERE id = %s",
                (self.vault.encrypt(json.dumps(payload)), registration.email_id),
            )
        adapter = self.adapter()
        result = self.worker(adapter).send(registration.email_id, allow_real_email=True)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], "INVALID_EMAIL_PAYLOAD")
        adapter.send.assert_not_called()


class EmailDispatcherMySQLTests(MealServicesFixture):
    def queue_email(self):
        suffix = uuid4().hex
        return self.employees.register(
            self.admin_context, "AUTO-" + suffix[:20], "Fictional Automatic Email",
            "meal-dispatch-test-" + suffix + "@gmail.com", self.department_id,
        )

    def dispatcher(self, adapter, batch_size=10):
        from meal_management.email_dispatcher import EmailDispatcher
        from meal_management.email_worker import EmailDeliveryWorker
        from meal_management.security import QrRenderer

        worker = EmailDeliveryWorker(self.db, self.vault, QrRenderer(), adapter)
        return EmailDispatcher(self.db, worker, batch_size=batch_size)

    def test_bounded_batch_and_process_restart_only_send_queued_rows(self):
        first = self.queue_email()
        second = self.queue_email()
        failed = self.queue_email()
        cancelled = self.queue_email()
        with self.db.transaction() as tx:
            tx.execute("UPDATE email_queue SET status = %s, last_error = %s WHERE id = %s", ("FAILED", "EMAIL_DELIVERY_NEEDS_REVIEW", failed.email_id))
            tx.execute("UPDATE email_queue SET status = %s, payload_ciphertext = NULL WHERE id = %s", ("CANCELLED", cancelled.email_id))
        adapter = Mock(enabled=True, send=Mock(return_value="<fictional-accepted>"))
        self.assertEqual(self.dispatcher(adapter, batch_size=1).dispatch_once()["sent"], 1)
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (second.email_id,)), "QUEUED")
        self.assertEqual(self.dispatcher(adapter, batch_size=1).dispatch_once()["sent"], 1)
        self.assertEqual(self.dispatcher(adapter).dispatch_once()["selected"], 0)
        self.assertEqual([call.args[0].email_id for call in adapter.send.call_args_list], [first.email_id, second.email_id])
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (failed.email_id,)), "FAILED")
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (cancelled.email_id,)), "CANCELLED")

    def test_two_admin_dispatchers_share_durable_claim_without_duplicate_delivery(self):
        registration = self.queue_email()
        adapter = Mock(enabled=True, send=Mock(return_value="<fictional-accepted>"))
        barrier = threading.Barrier(2)
        def poll():
            barrier.wait(timeout=10)
            return self.dispatcher(adapter).dispatch_once()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(poll) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(result["sent"] for result in results), 1)
        adapter.send.assert_called_once()
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE id = %s", (registration.email_id,)), "SENT")
