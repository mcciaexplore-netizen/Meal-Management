import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class SharedScannerMySQLTests(MealServicesFixture):
    def shared(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        return ScanAppService(self.db, self.meals, ScanReceiptService(self.db))

    def profile(self):
        return self.rows("SELECT staff_id, scanner_id, meal_type_id FROM scan_app_settings WHERE id = 1")[0]

    def visitor(self):
        return {"company_name": "Example Company", "name": "Fictional Visitor", "email": "visitor@example.test", "phone": "+919876543210"}

    def allocated_master(self, meal_limit=5):
        return self.qr.issue_master(
            self.admin_context, company_name="Example Company", contact_name="Fictional Visitor",
            email="visitor@example.test", phone="+919876543210", meal_limit=meal_limit,
        )

    def test_shared_employee_scan_uses_system_actor_defaults_and_no_staff_session(self):
        registration, token = self.employee()
        profile = self.profile()
        browser = secrets.token_bytes(32)
        identifier = uuid4()
        sessions_before = self.scalar("SELECT COUNT(*) FROM staff_sessions")
        result = self.shared().read(browser, identifier, token)
        self.assertTrue(result.approved)
        self.assertEqual(len(result.meal_ids), 1)
        serving = self.rows("SELECT waiter_id, scanner_id, meal_type_id, employee_id FROM servings WHERE id = %s", (result.serving_id,))[0]
        self.assertEqual(serving["waiter_id"], profile["staff_id"])
        self.assertEqual(serving["scanner_id"], profile["scanner_id"])
        self.assertEqual(serving["meal_type_id"], profile["meal_type_id"])
        self.assertEqual(serving["employee_id"], registration.employee_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_sessions"), sessions_before)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_sessions WHERE staff_id = %s", (profile["staff_id"],)), 0)
        recovered = self.shared().result(browser, identifier)
        self.assertTrue(recovered["approved"])
        self.assertEqual(recovered["meal_ids"], list(result.meal_ids))

    def test_shared_master_flow_and_recovery_are_bound_to_one_browser(self):
        from meal_management.errors import DomainError

        credential = self.allocated_master()
        browser = secrets.token_bytes(32)
        other_browser = secrets.token_bytes(32)
        identifier = uuid4()
        result = self.shared().read(browser, identifier, credential.token)
        self.assertTrue(result.approved)
        for action in (
            lambda: self.shared().read(other_browser, identifier, credential.token),
            lambda: self.shared().result(other_browser, identifier),
        ):
            with self.assertRaises(DomainError) as caught:
                action()
            self.assertEqual(caught.exception.code, "REQUEST_MISMATCH")
        duplicate = self.shared().read(browser, identifier, credential.token)
        self.assertTrue(duplicate.approved)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.meal_ids, result.meal_ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (identifier.bytes,)), 1)
        self.assertEqual(self.shared().result(browser, identifier)["meal_ids"], list(result.meal_ids))

    def test_pending_request_keeps_captured_defaults_when_profile_changes(self):
        credential = self.allocated_master(2)
        browser = secrets.token_bytes(32)
        identifier = uuid4()
        original_profile = self.profile()
        self.shared()._reserve(browser, identifier)
        try:
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE scan_app_settings SET scanner_id = %s, meal_type_id = %s WHERE id = 1",
                    (self.scanner_id, self.meal_type_id),
                )
            result = self.shared().read(browser, identifier, credential.token)
            self.assertTrue(result.approved)
            serving = self.rows("SELECT scanner_id, meal_type_id FROM servings WHERE id = %s", (result.serving_id,))[0]
            self.assertEqual(serving["scanner_id"], original_profile["scanner_id"])
            self.assertEqual(serving["meal_type_id"], original_profile["meal_type_id"])
            second = self.shared().read(browser, uuid4(), credential.token)
            self.assertTrue(second.approved)
            current = self.rows("SELECT scanner_id, meal_type_id FROM servings WHERE id = %s", (second.serving_id,))[0]
            self.assertEqual(current["scanner_id"], self.scanner_id)
            self.assertEqual(current["meal_type_id"], self.meal_type_id)
        finally:
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE scan_app_settings SET scanner_id = %s, meal_type_id = %s WHERE id = 1",
                    (original_profile["scanner_id"], original_profile["meal_type_id"]),
                )

    def test_two_browsers_racing_for_uuid_cannot_share_ownership_or_create_two_meals(self):
        from meal_management.errors import DomainError

        registration, token = self.employee()
        identifier = uuid4()
        browsers = (secrets.token_bytes(32), secrets.token_bytes(32))
        barrier = threading.Barrier(2)

        def read(browser):
            barrier.wait(timeout=10)
            try:
                return browser, self.shared().read(browser, identifier, token)
            except DomainError as error:
                return browser, error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read, browser) for browser in browsers]
            results = [future.result(timeout=30) for future in futures]
        approved = [(browser, result) for browser, result in results if not isinstance(result, str) and result.approved]
        rejected = [result for _, result in results if isinstance(result, str)]
        self.assertEqual(len(approved), 1)
        self.assertEqual(rejected, ["REQUEST_MISMATCH"])
        bound = self.rows("SELECT browser_hash FROM scan_app_requests WHERE request_id = %s", (identifier.bytes,))[0]
        self.assertEqual(bytes(bound["browser_hash"]), approved[0][0])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 1)

    def test_same_browser_simultaneous_master_submissions_return_one_original_meal(self):
        credential = self.allocated_master(1)
        identifier = uuid4()
        browser = secrets.token_bytes(32)
        barrier = threading.Barrier(2)

        def record():
            barrier.wait(timeout=10)
            return self.shared().read(browser, identifier, credential.token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(result.approved for result in results))
        self.assertEqual(sum(not result.duplicate for result in results), 1)
        self.assertEqual(results[0].meal_ids, results[1].meal_ids)

    def test_master_allowance_accepts_each_person_once_then_expires(self):
        credential = self.allocated_master(5)
        browser = secrets.token_bytes(32)
        identifiers = [uuid4() for _ in range(6)]
        results = [self.shared().read(browser, identifier, credential.token) for identifier in identifiers]
        self.assertTrue(all(result.approved for result in results[:5]))
        self.assertFalse(results[5].approved)
        self.assertEqual(results[5].code, "QR_EXPIRED")
        retry = self.shared().read(browser, identifiers[4], credential.token)
        self.assertTrue(retry.approved)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.meal_ids, results[4].meal_ids)
        allocation = self.rows("SELECT meals_used, meal_limit, exhausted_at FROM master_qr_allocations WHERE qr_id = %s", (credential.qr_id,))[0]
        self.assertEqual((allocation["meals_used"], allocation["meal_limit"]), (5, 5))
        self.assertIsNotNone(allocation["exhausted_at"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 5)

    def test_simultaneous_final_allowance_approves_only_one_distinct_serving(self):
        credential = self.allocated_master(1)
        barrier = threading.Barrier(2)

        def record(browser):
            barrier.wait(timeout=10)
            return self.shared().read(browser, uuid4(), credential.token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=30) for future in [
                pool.submit(record, secrets.token_bytes(32)), pool.submit(record, secrets.token_bytes(32)),
            ]]
        self.assertEqual(sum(result.approved for result in results), 1)
        self.assertEqual([result.code for result in results if not result.approved], ["QR_EXPIRED"])
        self.assertEqual(self.scalar("SELECT meals_used FROM master_qr_allocations WHERE qr_id = %s", (credential.qr_id,)), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_allocated_master_cannot_bypass_group_details_or_limit_through_staff_scans(self):
        credential = self.allocated_master(1)
        details = self.meals.record(
            self.waiter_context,
            replace(self.scan(credential.token), visitor_details=self.visitor()),
        )
        self.assertFalse(details.approved)
        self.assertEqual(details.code, "MASTER_ALLOCATION_SCOPE_MISMATCH")
        quantity = self.meals.record(self.waiter_context, replace(self.scan(credential.token), quantity=2))
        self.assertFalse(quantity.approved)
        self.assertEqual(quantity.code, "VISITOR_QUANTITY_MUST_BE_ONE")
        self.assertEqual(self.scalar("SELECT meals_used FROM master_qr_allocations WHERE qr_id = %s", (credential.qr_id,)), 0)
        approved = self.meals.record(self.waiter_context, self.scan(credential.token))
        self.assertTrue(approved.approved)
        exhausted = self.shared().read(secrets.token_bytes(32), uuid4(), credential.token)
        self.assertFalse(exhausted.approved)
        self.assertEqual(exhausted.code, "QR_EXPIRED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_system_scanner_cannot_login_or_use_employee_admin_reports_or_authorizations(self):
        from meal_management.errors import DomainError
        from meal_management.models import ScanAppContext
        from meal_management.queries import QueryService
        from meal_management.reports import ReportService

        profile = self.profile()
        context = ScanAppContext(profile["staff_id"])
        credential = self.allocated_master(1)
        email = self.scalar("SELECT email FROM staff_accounts WHERE id = %s", (profile["staff_id"],))
        with self.assertRaises(DomainError) as caught:
            self.staff.authenticate(email, secrets.token_urlsafe(32))
        self.assertEqual(caught.exception.code, "INVALID_CREDENTIALS")
        start = datetime.now(timezone.utc) - timedelta(days=1)
        actions = (
            lambda: QueryService(self.db).list_employees(context),
            lambda: self.qr.issue_master(context),
            lambda: self.staff.create_staff(context, "Unauthorized", "unauthorized@example.test", secrets.token_urlsafe(32), {"ADMIN"}),
            lambda: ReportService(self.db).meal_report(context, start, start + timedelta(days=2)),
            lambda: self.approvals.authorize(context, uuid4(), credential.token, self.waiter_id, "TEST-SCANNER", self.meal_type_id, 1, "Visitor"),
        )
        for action in actions:
            with self.assertRaises(DomainError) as caught:
                action()
            self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_sessions WHERE staff_id = %s", (profile["staff_id"],)), 0)

    def test_system_scanner_has_no_password_and_cannot_gain_sessions_or_admin_role_in_database(self):
        import mysql.connector

        from meal_management.security import generate_token, token_digest

        profile = self.profile()
        staff = self.rows("SELECT password_hash, is_scanner FROM staff_accounts WHERE id = %s", (profile["staff_id"],))[0]
        self.assertIsNone(staff["password_hash"])
        self.assertTrue(staff["is_scanner"])
        self.assertEqual(self.rows("SELECT role_code FROM staff_account_roles WHERE staff_id = %s", (profile["staff_id"],)), [{"role_code": "WAITER"}])
        with self.assertRaises(mysql.connector.Error):
            with self.db.transaction() as tx:
                tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (profile["staff_id"],))
        with self.assertRaises(mysql.connector.Error):
            with self.db.transaction() as tx:
                now = tx.now()
                tx.execute(
                    "INSERT INTO staff_sessions (staff_id, token_hash, created_at, expires_at, last_seen_at) VALUES (%s, %s, %s, %s, %s)",
                    (profile["staff_id"], token_digest(generate_token()), now, now + timedelta(hours=1), now),
                )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_sessions WHERE staff_id = %s", (profile["staff_id"],)), 0)

    def test_invalid_shared_scan_has_system_attribution_without_creating_sessions(self):
        profile = self.profile()
        result = self.shared().record_invalid(secrets.token_bytes(32))
        self.assertEqual(result.code, "INVALID_INPUT")
        row = self.rows("SELECT staff_id, scanner_id, rejection_code, request_id FROM scan_attempts ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row["staff_id"], profile["staff_id"])
        self.assertEqual(row["scanner_id"], profile["scanner_id"])
        self.assertEqual(row["rejection_code"], "INVALID_INPUT")
        self.assertIsNone(row["request_id"])

    def test_browser_binding_is_immutable_at_database_level(self):
        import mysql.connector

        _, token = self.employee()
        identifier = uuid4()
        browser = secrets.token_bytes(32)
        self.assertTrue(self.shared().read(browser, identifier, token).approved)
        with self.assertRaises(mysql.connector.Error):
            with self.db.transaction() as tx:
                tx.execute("UPDATE scan_app_requests SET browser_hash = %s WHERE request_id = %s", (secrets.token_bytes(32), identifier.bytes))
        self.assertTrue(self.shared().result(browser, identifier)["approved"])

    def test_shared_meal_failure_keeps_browser_binding_without_approval_or_partial_visitor_rows(self):
        credential = self.allocated_master(1)
        identifier = uuid4()
        browser = secrets.token_bytes(32)
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "CREATE TRIGGER integration_fail_shared_meal BEFORE INSERT ON meals FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Intentional shared meal failure'"
            )
        finally:
            cursor.close()
        try:
            result = self.shared().read(browser, identifier, credential.token)
        finally:
            cursor = self.admin_connection.cursor()
            try:
                cursor.execute("DROP TRIGGER integration_fail_shared_meal")
            finally:
                cursor.close()
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (identifier.bytes,)), 0)
        self.assertEqual(bytes(self.scalar("SELECT browser_hash FROM scan_app_requests WHERE request_id = %s", (identifier.bytes,))), browser)
        self.assertFalse(self.shared().result(browser, identifier)["approved"])
        self.assertEqual(self.scalar("SELECT meals_used FROM master_qr_allocations WHERE qr_id = %s", (credential.qr_id,)), 0)
        self.assertTrue(self.shared().read(browser, identifier, credential.token).approved)
