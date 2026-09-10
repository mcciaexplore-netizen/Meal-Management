import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from meal_management.email_actions import EmailActions
from meal_management.errors import DomainError
from meal_management.models import ServerContext
from meal_management.runtime import RuntimeSettings


class EmailActionsTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeSettings(
            csrf_secret=b"a" * 32, login_rate_secret=b"b" * 32,
            email_backend="gmail", email_send_enabled=True,
        )
        self.context = ServerContext("fictional-session-token")
        self.queue = Mock()
        self.queue.approve_single.side_effect = lambda context, email_id, employee_id: email_id
        self.queue.approve_bulk.side_effect = lambda context, identifiers: {"email_ids": list(identifiers), "approved_count": len(identifiers)}
        self.queue.validate_approved_bulk.side_effect = lambda context, identifiers: list(identifiers)
        self.services = SimpleNamespace(database=Mock(), qr=SimpleNamespace(vault=Mock(), renderer=Mock()), email_queue=self.queue)
        self.actions = EmailActions(self.runtime, self.services)
        self.worker = Mock()
        self.worker.send.side_effect = lambda identifier, **options: {"email_id": identifier, "status": "SENT", "code": None}
        self.actions._worker = Mock(return_value=self.worker)

    def test_constructor_does_not_create_provider_worker_or_query_database(self):
        with (
            patch("meal_management.email_actions.delivery_from_settings") as provider,
            patch("meal_management.email_actions.EmailDeliveryWorker") as worker,
        ):
            EmailActions(self.runtime, self.services)
        provider.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(self.services.database.mock_calls, [])
        self.assertEqual(self.queue.mock_calls, [])

    def test_disabled_and_preview_modes_fail_before_any_approval_or_delivery(self):
        for runtime in (
            replace(self.runtime, email_send_enabled=False),
            replace(self.runtime, email_backend="preview"),
            replace(self.runtime, email_send_enabled=1),
        ):
            for method, arguments in (("send_single", (7, 9)), ("approve_bulk", ([9],)), ("process_bulk", ([9],))):
                with self.subTest(runtime=runtime, method=method):
                    actions = EmailActions(runtime, self.services)
                    with patch.object(actions, "_worker") as worker:
                        with self.assertRaisesRegex(DomainError, "^REAL_EMAIL_NOT_AUTHORIZED$"):
                            getattr(actions, method)(self.context, *arguments)
                    worker.assert_not_called()
        self.assertEqual(self.queue.mock_calls, [])
        self.assertEqual(self.services.database.mock_calls, [])

    def test_single_sends_only_after_bound_admin_approval_commits(self):
        events = []
        self.queue.approve_single.side_effect = lambda *args: events.append(("approved", args)) or 9
        self.worker.send.side_effect = lambda *args, **kwargs: events.append(("sent", args, kwargs)) or {"email_id": 9, "status": "SENT", "code": None}
        self.assertEqual(self.actions.send_single(self.context, 7, 9), {"email_id": 9, "status": "SENT", "code": None})
        self.assertEqual(events, [("approved", (self.context, 9, 7)), ("sent", (9,), {"allow_real_email": True})])
        self.queue.approve_bulk.assert_not_called()

    def test_single_approval_auth_or_commit_failure_never_builds_sender(self):
        for error in (DomainError("ROLE_REQUIRED"), DomainError("EMAIL_EMPLOYEE_MISMATCH"), RuntimeError("private database details")):
            with self.subTest(error=type(error)):
                self.queue.approve_single.side_effect = error
                with self.assertRaises(type(error)):
                    self.actions.send_single(self.context, 7, 9)
                self.actions._worker.assert_not_called()
                self.worker.send.assert_not_called()

    def test_single_replay_uses_same_email_identifier_and_existing_worker_result(self):
        self.worker.send.side_effect = [
            {"email_id": 9, "status": "SENT", "code": None},
            {"email_id": 9, "status": "ALREADY_SENT", "code": None},
        ]
        self.assertEqual(self.actions.send_single(self.context, 7, 9)["status"], "SENT")
        self.assertEqual(self.actions.send_single(self.context, 7, 9)["status"], "ALREADY_SENT")
        self.assertEqual(self.worker.send.call_args_list, [call(9, allow_real_email=True), call(9, allow_real_email=True)])

    def test_single_rejects_unconfirmed_approval_result_before_delivery(self):
        self.queue.approve_single.side_effect = None
        for approval in (None, 10, True):
            self.queue.approve_single.return_value = approval
            with self.assertRaisesRegex(DomainError, "^EMAIL_APPROVAL_UNCONFIRMED$"):
                self.actions.send_single(self.context, 7, 9)
        self.actions._worker.assert_not_called()

    def test_bulk_approval_only_persists_explicit_ids_without_starting_sender(self):
        result = self.actions.approve_bulk(self.context, [9, 10])
        self.assertEqual(result, {"email_ids": [9, 10], "approved_count": 2})
        self.queue.approve_bulk.assert_called_once_with(self.context, [9, 10])
        self.actions._worker.assert_not_called()
        self.worker.send.assert_not_called()

    def test_bulk_processing_validates_every_id_before_any_send(self):
        events = []
        def validate(context, identifiers):
            self.assertIs(context, self.context)
            self.worker.send.assert_not_called()
            events.append(("validated", list(identifiers)))
            return identifiers
        def send(identifier, **options):
            self.assertEqual(events[0], ("validated", [9, 10]))
            events.append(("sent", identifier))
            return {"email_id": identifier, "status": "SENT", "code": None}
        self.queue.validate_approved_bulk.side_effect = validate
        self.worker.send.side_effect = send
        result = self.actions.process_bulk(self.context, [9, 10])
        self.assertEqual([item["email_id"] for item in result["results"]], [9, 10])
        self.assertEqual(events, [("validated", [9, 10]), ("sent", 9), ("sent", 10)])
        self.queue.approve_bulk.assert_not_called()
        self.queue.approve_single.assert_not_called()

    def test_one_invalid_or_unauthorized_bulk_id_prevents_all_sends(self):
        for code in ("ROLE_REQUIRED", "EMAIL_APPROVAL_REQUIRED", "INVALID_EMAIL_MODE", "EMAIL_NOT_FOUND", "EMAIL_NOT_DELIVERABLE"):
            self.queue.validate_approved_bulk.side_effect = DomainError(code)
            with self.assertRaisesRegex(DomainError, code):
                self.actions.process_bulk(self.context, [9, 10])
        self.actions._worker.assert_not_called()
        self.worker.send.assert_not_called()

    def test_bulk_identifier_input_is_bounded_unique_and_strict(self):
        for identifiers in (None, [], list(range(1, 12)), [1, 1], [True], [0], [-1], ["9"], [2**64], {9}, "9"):
            with self.subTest(identifiers=identifiers):
                with self.assertRaisesRegex(DomainError, "^INVALID_EMAIL_IDS$"):
                    self.actions.process_bulk(self.context, identifiers)
        self.queue.validate_approved_bulk.assert_not_called()
        self.actions._worker.assert_not_called()

    def test_bulk_preflight_must_confirm_exact_identifiers(self):
        self.queue.validate_approved_bulk.side_effect = None
        for approved in (None, [9], [9, 11], [9, 9], [True, 10], ["9", 10]):
            self.queue.validate_approved_bulk.return_value = approved
            with self.assertRaisesRegex(DomainError, "^EMAIL_APPROVAL_UNCONFIRMED$"):
                self.actions.process_bulk(self.context, [9, 10])
        self.actions._worker.assert_not_called()

    def test_bulk_returns_sent_failed_and_uncertain_original_outcomes(self):
        outcomes = [
            {"email_id": 9, "status": "ALREADY_SENT", "code": None},
            {"email_id": 10, "status": "FAILED", "code": "EMAIL_AUTHENTICATION_FAILED"},
            {"email_id": 11, "status": "NEEDS_REVIEW", "code": "EMAIL_DELIVERY_NEEDS_REVIEW"},
        ]
        self.worker.send.side_effect = outcomes
        self.assertEqual(self.actions.process_bulk(self.context, [9, 10, 11]), {"results": outcomes})
        self.assertEqual(self.worker.send.call_count, 3)

    def test_bulk_partial_result_is_preserved_after_ambiguous_claim_failure(self):
        self.worker.send.side_effect = [
            {"email_id": 9, "status": "SENT", "code": None},
            DomainError("EMAIL_CLAIM_UNCONFIRMED"),
        ]
        self.assertEqual(self.actions.process_bulk(self.context, [9, 10]), {"results": [
            {"email_id": 9, "status": "SENT", "code": None},
            {"email_id": 10, "status": "NEEDS_REVIEW", "code": "EMAIL_CLAIM_UNCONFIRMED"},
        ]})

    def test_provider_or_database_details_are_never_exposed_in_delivery_results(self):
        for error in (RuntimeError("private-password-recipient"), DomainError("private-password-recipient")):
            self.worker.send.side_effect = error
            result = self.actions.send_single(self.context, 7, 9)
            self.assertEqual(result, {"email_id": 9, "status": "NEEDS_REVIEW", "code": "EMAIL_DELIVERY_NEEDS_REVIEW"})

    def test_malformed_worker_results_cannot_report_approval_or_expose_extra_fields(self):
        self.worker.send.side_effect = None
        for result in (None, {}, {"email_id": 10, "status": "SENT"}, {"email_id": 9, "status": "UNKNOWN"}):
            self.worker.send.return_value = result
            self.assertEqual(self.actions.send_single(self.context, 7, 9)["status"], "NEEDS_REVIEW")
        self.worker.send.return_value = {"email_id": 9, "status": "FAILED", "code": "private-provider-message", "recipient": "private-recipient"}
        self.assertEqual(self.actions.send_single(self.context, 7, 9), {"email_id": 9, "status": "FAILED", "code": "EMAIL_DELIVERY_FAILED"})

    def test_worker_factory_uses_existing_services_and_runtime_provider(self):
        actions = EmailActions(self.runtime, self.services)
        with (
            patch("meal_management.email_actions.delivery_from_settings") as provider,
            patch("meal_management.email_actions.EmailDeliveryWorker") as worker,
        ):
            result = actions._worker()
        provider.assert_called_once_with(self.runtime)
        worker.assert_called_once_with(self.services.database, self.services.qr.vault, self.services.qr.renderer, provider.return_value)
        self.assertIs(result, worker.return_value)
        self.assertEqual(self.services.database.mock_calls, [])
