import unittest
from unittest.mock import Mock
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.models import ServerContext
from test_api import ApiHarness


class EmployeeEmailApiTests(unittest.TestCase):
    def setUp(self):
        self.harness = ApiHarness()
        self.addCleanup(self.harness.client.close)
        self.client = self.harness.client
        self.actions = Mock()
        self.actions.send_single.return_value = {"email_id": 19, "status": "SENT", "code": None}
        self.actions.approve_bulk.return_value = {"email_ids": [20, 21], "approved_count": 2}
        self.actions.process_bulk.return_value = {"results": [{"email_id": 20, "status": "SENT", "code": None}]}
        self.harness.app.state.email_actions = self.actions
        self.bulk = {
            "request_id": str(uuid4()),
            "employees": [{"employee_code": "TEST-ONE", "full_name": "Fictional Employee", "email": "one@example.test", "department_id": 1}],
        }
        self.harness.services.employees.register_bulk.return_value = {
            "batch_id": 4, "employees": [{"employee_id": 7, "qr_id": 101, "email_id": 20}], "replayed": False,
        }

    def mutation_cases(self):
        return (
            ("/api/employees/7/emails/19/send", None),
            ("/api/employees/bulk", self.bulk),
            ("/api/email-queue/approve", {"email_ids": [20, 21]}),
            ("/api/email-queue/process", {"email_ids": [20]}),
        )

    def test_new_mutations_require_login(self):
        for path, body in self.mutation_cases():
            with self.subTest(path=path):
                response = self.harness.post(path, json=body)
                self.assertEqual(response.status_code, 401)
        self.actions.send_single.assert_not_called()
        self.actions.approve_bulk.assert_not_called()
        self.actions.process_bulk.assert_not_called()
        self.harness.services.employees.register_bulk.assert_not_called()

    def test_waiters_and_auditors_cannot_send_approve_import_or_read_status(self):
        for role in ("waiter", "auditor"):
            self.harness.login(role)
            for path, body in self.mutation_cases():
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.harness.post(path, json=body).status_code, 403)
            for path in ("/api/employees/7/emails/19", "/api/email-queue/19"):
                self.assertEqual(self.client.get(path).status_code, 403)
        self.actions.send_single.assert_not_called()
        self.actions.approve_bulk.assert_not_called()
        self.actions.process_bulk.assert_not_called()

    def test_every_mutation_requires_csrf(self):
        self.harness.login()
        for path, body in self.mutation_cases():
            with self.subTest(path=path):
                response = self.client.post(path, json=body, headers={"Origin": self.harness.runtime.app_origin})
                self.assertEqual(response.status_code, 403)
        self.actions.send_single.assert_not_called()
        self.actions.approve_bulk.assert_not_called()
        self.actions.process_bulk.assert_not_called()

    def test_single_send_uses_authenticated_context_and_exact_email(self):
        self.harness.login()
        response = self.harness.post("/api/employees/7/emails/19/send")
        self.assertEqual(response.json(), {"email_id": 19, "status": "SENT", "code": None})
        context, employee_id, email_id = self.actions.send_single.call_args.args
        self.assertIsInstance(context, ServerContext)
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)
        self.assertEqual((employee_id, email_id), (7, 19))
        self.harness.services.qr.resend_employee.assert_not_called()
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_failed_or_uncertain_provider_result_is_not_reported_as_sent(self):
        self.harness.login()
        for status in ("FAILED", "NEEDS_REVIEW", "CANCELLED", "ALREADY_SENT"):
            self.actions.send_single.return_value = {"email_id": 19, "status": status, "code": None}
            self.assertEqual(self.harness.post("/api/employees/7/emails/19/send").json()["status"], status)

    def test_repeated_send_calls_keep_the_original_email_id(self):
        self.harness.login()
        self.actions.send_single.side_effect = [
            {"email_id": 19, "status": "SENT", "code": None},
            {"email_id": 19, "status": "ALREADY_SENT", "code": None},
        ]
        self.assertEqual(self.harness.post("/api/employees/7/emails/19/send").json()["status"], "SENT")
        self.assertEqual(self.harness.post("/api/employees/7/emails/19/send").json()["status"], "ALREADY_SENT")
        self.assertEqual([call.args[2] for call in self.actions.send_single.call_args_list], [19, 19])

    def test_transaction_failure_returns_safe_error(self):
        self.harness.login()
        for action, path, body in (
            (self.actions.send_single, "/api/employees/7/emails/19/send", None),
            (self.actions.approve_bulk, "/api/email-queue/approve", {"email_ids": [20]}),
            (self.harness.services.employees.register_bulk, "/api/employees/bulk", self.bulk),
        ):
            action.side_effect = RuntimeError("private database details")
            response = self.harness.post(path, json=body)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private database details", response.text)

    def test_unapproved_bulk_send_and_employee_mismatch_return_rejection(self):
        self.harness.login()
        self.actions.process_bulk.side_effect = DomainError("EMAIL_APPROVAL_REQUIRED")
        self.assertEqual(self.harness.post("/api/email-queue/process", json={"email_ids": [20]}).status_code, 400)
        self.actions.send_single.side_effect = DomainError("EMAIL_NOT_FOUND")
        self.assertEqual(self.harness.post("/api/employees/8/emails/19/send").status_code, 404)

    def test_status_recovery_is_scoped_and_does_not_send(self):
        self.harness.login()
        self.harness.services.email_queue.status.return_value = {"email_id": 19, "status": "SENT", "delivery_mode": "SINGLE"}
        response = self.client.get("/api/employees/7/emails/19")
        self.assertEqual(response.json()["status"], "SENT")
        self.assertEqual(self.harness.services.email_queue.status.call_args.kwargs, {"employee_id": 7})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.actions.send_single.assert_not_called()

    def test_single_registration_and_bulk_import_do_not_send(self):
        self.harness.login()
        self.assertEqual(self.harness.post("/api/employees", json=self.bulk["employees"][0]).status_code, 201)
        response = self.harness.post("/api/employees/bulk", json=self.bulk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["batch_id"], 4)
        context, request_id, employees = self.harness.services.employees.register_bulk.call_args.args
        self.assertEqual(request_id, self.bulk["request_id"])
        self.assertEqual(employees, self.bulk["employees"])
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)
        self.actions.send_single.assert_not_called()
        self.actions.process_bulk.assert_not_called()

    def test_bulk_replay_preserves_result(self):
        self.harness.login()
        original = self.harness.services.employees.register_bulk.return_value
        self.harness.services.employees.register_bulk.side_effect = [original, {**original, "replayed": True}]
        first = self.harness.post("/api/employees/bulk", json=self.bulk).json()
        second = self.harness.post("/api/employees/bulk", json=self.bulk).json()
        self.assertEqual(first["employees"], second["employees"])
        self.assertTrue(second["replayed"])

    def test_approval_does_not_itself_call_processing(self):
        self.harness.login()
        result = self.harness.post("/api/email-queue/approve", json={"email_ids": [20, 21]})
        self.assertEqual(result.json(), {"email_ids": [20, 21], "approved_count": 2})
        self.assertEqual(self.actions.approve_bulk.call_args.args[1], [20, 21])
        self.actions.process_bulk.assert_not_called()

    def test_processing_routes_only_requested_ids(self):
        self.harness.login()
        response = self.harness.post("/api/email-queue/process", json={"email_ids": [20]})
        self.assertEqual(response.json()["results"][0]["email_id"], 20)
        self.assertEqual(self.actions.process_bulk.call_args.args[1], [20])

    def test_invalid_bulk_inputs_and_staff_identity_are_rejected(self):
        self.harness.login()
        for body in (
            {**self.bulk, "employees": []},
            {**self.bulk, "employees": self.bulk["employees"] * 101},
            {**self.bulk, "request_id": "not-a-uuid"},
            {**self.bulk, "approved_by_staff_id": 1},
            {**self.bulk, "employees": [{**self.bulk["employees"][0], "department_id": True}]},
            {**self.bulk, "employees": [{**self.bulk["employees"][0], "role": "ADMIN"}]},
        ):
            with self.subTest(body_keys=list(body)):
                self.assertEqual(self.harness.post("/api/employees/bulk", json=body).status_code, 422)
        self.harness.services.employees.register_bulk.assert_not_called()

    def test_email_ids_are_bounded_unique_strict_and_not_client_approved(self):
        self.harness.login()
        for path, maximum in (("/api/email-queue/approve", 100), ("/api/email-queue/process", 10)):
            for values in ([], [1, 1], [True], ["1"], [0], list(range(1, maximum + 2))):
                self.assertEqual(self.harness.post(path, json={"email_ids": values}).status_code, 422)
            self.assertEqual(self.harness.post(path, json={"email_ids": [20], "approved_by": 1}).status_code, 422)
        self.actions.approve_bulk.assert_not_called()
        self.actions.process_bulk.assert_not_called()

    def test_queue_and_employee_history_have_different_scopes(self):
        self.harness.login()
        self.client.get("/api/email-queue")
        self.assertEqual(self.harness.queries.email_status.call_args.kwargs["delivery_scope"], "bulk")
        self.client.get("/api/email-queue?employee_id=7")
        self.assertEqual(self.harness.queries.email_status.call_args.kwargs["delivery_scope"], "all")

    def test_scanner_has_no_email_or_import_routes(self):
        scanner = ApiHarness(application="scanner")
        self.addCleanup(scanner.client.close)
        for path, body in self.mutation_cases():
            response = scanner.client.post(path, json=body, headers={"Origin": scanner.runtime.app_origin})
            self.assertEqual(response.status_code, 404)
        for path in ("/api/employees/7/emails/19", "/api/email-queue/19"):
            self.assertEqual(scanner.client.get(path).status_code, 404)
        scanner.services.database.transaction.assert_not_called()

    def test_queue_can_review_one_batch_and_validates_filter(self):
        self.harness.login()
        self.assertEqual(self.client.get("/api/email-queue?bulk_batch_id=4").status_code, 200)
        self.assertEqual(self.harness.queries.email_status.call_args.kwargs["bulk_batch_id"], 4)
        for invalid in ("0", "-1", "invalid"):
            self.assertEqual(self.client.get("/api/email-queue?bulk_batch_id=" + invalid).status_code, 422)


if __name__ == "__main__":
    unittest.main()
