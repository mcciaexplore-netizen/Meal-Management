import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class ScanReceiptsMySQLTests(MealServicesFixture):
    def receipts(self):
        from meal_management.scan_receipts import ScanReceiptService

        return ScanReceiptService(self.db)

    def assert_receipt_error(self, context, request_id, code):
        from meal_management.errors import DomainError

        for method in (self.receipts().get, self.receipts().photo_key):
            with self.subTest(method=method.__name__):
                with self.assertRaises(DomainError) as caught:
                    method(context, request_id)
                self.assertEqual(caught.exception.code, code)

    def test_admin_and_waiter_can_scan_reusable_employee_qr_with_distinct_receipts(self):
        registration, token = self.employee()
        first_scan = self.scan(token)
        second_scan = self.scan(token)
        admin_result = self.meals.record(self.admin_context, first_scan)
        waiter_result = self.meals.record(self.waiter_context, second_scan)
        replay = self.meals.record(self.admin_context, first_scan)
        self.assertTrue(admin_result.approved)
        self.assertTrue(waiter_result.approved)
        self.assertTrue(replay.approved)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.meal_ids, admin_result.meal_ids)
        self.assertEqual(replay.serving_id, admin_result.serving_id)
        self.assertNotEqual(admin_result.serving_id, waiter_result.serving_id)
        for context, scan, expected in (
            (self.admin_context, first_scan, admin_result),
            (self.waiter_context, second_scan, waiter_result),
        ):
            receipt = self.receipts().get(context, scan.request_id)
            recovered = self.receipts().result(context, scan.request_id)
            self.assertEqual(receipt["employee_id"], registration.employee_id)
            self.assertEqual(receipt["employee_name"], "Fictional Employee")
            self.assertTrue(receipt["photo_available"])
            self.assertEqual(receipt["serving_id"], expected.serving_id)
            self.assertEqual(receipt["kind"], "EMPLOYEE")
            self.assertEqual(receipt["served_at"].utcoffset(), timedelta(0))
            self.assertEqual(recovered["meal_ids"], list(expected.meal_ids))
            self.assertTrue(recovered["approved"])
            self.assertTrue(recovered["duplicate"])
            self.assertTrue(self.receipts().photo_key(context, scan.request_id).startswith("selfies/"))
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 2
        )

    def test_concurrent_admin_retries_recover_one_original_receipt(self):
        registration, token = self.employee()
        scan = self.scan(token)
        barrier = threading.Barrier(2)

        def record():
            barrier.wait(timeout=10)
            return self.meals.record(self.admin_context, scan)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(result.approved for result in results))
        self.assertEqual(sum(not result.duplicate for result in results), 1)
        recovered = self.receipts().result(self.admin_context, scan.request_id)
        self.assertEqual(recovered["meal_ids"], list(results[0].meal_ids))
        self.assertEqual(results[0].meal_ids, results[1].meal_ids)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 1
        )

    def test_other_caller_cannot_recover_receipt_result_or_photo_even_when_admin(self):
        from meal_management.errors import DomainError

        _, token = self.employee()
        scan = self.scan(token)
        self.assertTrue(self.meals.record(self.waiter_context, scan).approved)
        self.assert_receipt_error(self.admin_context, scan.request_id, "SCAN_RECEIPT_NOT_FOUND")
        with self.assertRaises(DomainError) as caught:
            self.receipts().result(self.admin_context, scan.request_id)
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")
        rejected_collision = self.meals.record(self.admin_context, scan)
        self.assertFalse(rejected_collision.approved)
        self.assert_receipt_error(self.admin_context, scan.request_id, "SCAN_RECEIPT_NOT_FOUND")
        with self.assertRaises(DomainError):
            self.receipts().result(self.admin_context, scan.request_id)

    def test_inactive_and_revoked_employee_scans_have_rejections_without_receipts(self):
        for condition in ("inactive", "revoked"):
            with self.subTest(condition=condition):
                registration, token = self.employee()
                if condition == "inactive":
                    self.employees.set_active(self.admin_context, registration.employee_id, False)
                else:
                    self.qr.revoke(self.admin_context, registration.qr_id, "Integration revocation")
                scan = self.scan(token)
                result = self.meals.record(self.admin_context, scan)
                self.assertFalse(result.approved)
                self.assert_receipt_error(self.admin_context, scan.request_id, "SCAN_REJECTED")
                recovered = self.receipts().result(self.admin_context, scan.request_id)
                self.assertFalse(recovered["approved"])
                self.assertEqual(recovered["code"], result.code)
                self.assertNotIn("employee_name", recovered)
                self.assertNotIn("photo_available", recovered)

    def test_expired_employee_qr_cannot_reveal_employee_receipt(self):
        from meal_management.security import generate_token, token_digest

        registration, _ = self.employee()
        self.qr.revoke(self.admin_context, registration.qr_id, "Expired credential fixture")
        token = generate_token()
        with self.db.transaction() as tx:
            now = tx.now()
            tx.insert(
                "INSERT INTO qr_credentials "
                "(kind, employee_id, token_hash, token_ciphertext, issued_by, issued_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    "EMPLOYEE", registration.employee_id, token_digest(token), self.vault.encrypt(token),
                    self.admin_id, now - timedelta(days=2), now - timedelta(days=1),
                ),
            )
        scan = self.scan(token)
        result = self.meals.record(self.waiter_context, scan)
        self.assertFalse(result.approved)
        self.assertIn("EXPIRED", result.code)
        self.assert_receipt_error(self.waiter_context, scan.request_id, "SCAN_REJECTED")
        self.assertEqual(self.receipts().result(self.waiter_context, scan.request_id)["code"], result.code)

    def test_committed_receipt_and_result_survive_qr_revocation(self):
        registration, token = self.employee()
        scan = self.scan(token)
        original = self.meals.record(self.waiter_context, scan)
        self.qr.revoke(self.admin_context, registration.qr_id, "Revoke after confirmed serving")
        self.assertEqual(self.receipts().get(self.waiter_context, scan.request_id)["serving_id"], original.serving_id)
        self.assertEqual(self.receipts().result(self.waiter_context, scan.request_id)["meal_ids"], list(original.meal_ids))

    def test_legacy_authorized_master_request_retains_visitor_receipt(self):
        from meal_management.errors import DomainError

        credential = self.qr.issue_master(self.admin_context)
        unauthorized_scan = self.scan(credential.token, quantity=2)
        self.assertFalse(self.meals.record(self.admin_context, unauthorized_scan).approved)
        self.assert_receipt_error(self.admin_context, unauthorized_scan.request_id, "SCAN_PENDING")
        request_id = uuid4()
        authorization_id = self.approvals.authorize(
            self.admin_context, request_id, credential.token, self.admin_id, "TEST-SCANNER",
            self.meal_type_id, 2, "Fictional Admin Visitor", visitor_organization="Example Organization",
        )
        scan = self.scan(credential.token, request_id, 2, authorization_id)
        pending = self.receipts().result(self.admin_context, request_id)
        self.assertFalse(pending["approved"])
        self.assertEqual(pending["code"], "PROCESSING_UNCONFIRMED")
        self.assert_receipt_error(self.admin_context, request_id, "SCAN_PENDING")
        result = self.meals.record(self.admin_context, scan)
        self.assertTrue(result.approved)
        receipt = self.receipts().get(self.admin_context, request_id)
        self.assertEqual(receipt["kind"], "MASTER")
        self.assertEqual(receipt["quantity"], 2)
        self.assertEqual(receipt["visitor_name"], "Fictional Admin Visitor")
        self.assertIsNone(receipt["employee_id"])
        self.assertIsNone(receipt["employee_name"])
        self.assertFalse(receipt["photo_available"])
        with self.assertRaises(DomainError) as caught:
            self.receipts().photo_key(self.admin_context, request_id)
        self.assertEqual(caught.exception.code, "PHOTO_NOT_FOUND")
        self.assertEqual(self.receipts().result(self.admin_context, request_id)["meal_ids"], list(result.meal_ids))

    def test_legacy_waiter_master_receipt_requires_assigned_verified_authorization(self):
        _, scan = self.master_approval(quantity=3)
        wrong = self.meals.record(self.waiter_context, replace(scan, authorization_id=scan.authorization_id + 999))
        self.assertFalse(wrong.approved)
        self.assert_receipt_error(self.waiter_context, scan.request_id, "SCAN_PENDING")
        result = self.meals.record(self.waiter_context, scan)
        self.assertTrue(result.approved)
        self.assertEqual(self.receipts().get(self.waiter_context, scan.request_id)["visitor_name"], "Fictional Visitor")
        self.assertEqual(len(self.receipts().result(self.waiter_context, scan.request_id)["meal_ids"]), 3)

    def test_scan_access_does_not_grant_waiter_employee_admin_or_unrestricted_reports(self):
        from meal_management.errors import DomainError
        from meal_management.queries import QueryService
        from meal_management.reports import ReportService

        registration, token = self.employee()
        self.assertTrue(self.meals.record(self.waiter_context, self.scan(token)).approved)
        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = start + timedelta(days=2)
        actions = (
            lambda: self.employees.set_active(self.waiter_context, registration.employee_id, False),
            lambda: QueryService(self.db).list_employees(self.waiter_context),
            lambda: QueryService(self.db).meal_history(self.waiter_context, start, end),
            lambda: ReportService(self.db).meal_report(self.waiter_context, start, end),
            lambda: self.qr.retrieve(self.waiter_context, registration.qr_id),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(DomainError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "ROLE_REQUIRED")
        catalog = QueryService(self.db).catalog(self.admin_context)
        ids = {staff["id"] for staff in catalog["waiters"]}
        self.assertIn(self.admin_id, ids)
        self.assertIn(self.waiter_id, ids)
        self.assertEqual(QueryService(self.db).catalog(self.waiter_context)["waiters"], [])

    def test_deactivated_serving_staff_cannot_recover_previous_receipt(self):
        from meal_management.errors import DomainError

        suffix = uuid4().hex[:12]
        password = secrets.token_urlsafe(32)
        staff_id = self.staff.create_staff(
            self.admin_context, "Inactive Scan Staff", f"scan-{suffix}@example.test", password, {"WAITER"}
        )
        context = self.staff.authenticate(f"scan-{suffix}@example.test", password)
        _, token = self.employee()
        scan = self.scan(token)
        self.assertTrue(self.meals.record(context, scan).approved)
        self.staff.set_active(self.admin_context, staff_id, False)
        for method in (self.receipts().get, self.receipts().photo_key, self.receipts().result):
            with self.subTest(method=method.__name__):
                with self.assertRaises(DomainError):
                    method(context, scan.request_id)
