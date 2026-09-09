import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, timezone
from uuid import uuid4

from mysql_support import IsolatedMySQLTestCase, MYSQL_TESTS_ENABLED


@unittest.skipUnless(MYSQL_TESTS_ENABLED, "MySQL connection and disposable-schema changes are not enabled")
class MealServicesFixture(IsolatedMySQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.accounts import StaffService
        from meal_management.employees import EmployeeService
        from meal_management.meals import ApprovalService, MealService
        from meal_management.qr import QrService
        from meal_management.security import QrRenderer, TokenVault

        cls.staff = StaffService(cls.db)
        admin_password = secrets.token_urlsafe(32)
        waiter_password = secrets.token_urlsafe(32)
        cls.admin_id = cls.staff.bootstrap_admin("Test Administrator", "admin@example.test", admin_password)
        cls.admin_context = cls.staff.authenticate("admin@example.test", admin_password)
        cls.waiter_id = cls.staff.create_staff(
            cls.admin_context, "Test Waiter", "waiter@example.test", waiter_password, {"WAITER"}
        )
        cls.waiter_context = cls.staff.authenticate("waiter@example.test", waiter_password)
        cls.waiter_context_two = cls.staff.authenticate("waiter@example.test", waiter_password)
        cls.vault = TokenVault(cls.settings.qr_encryption_keys)
        cls.qr = QrService(cls.db, cls.vault, QrRenderer())
        cls.employees = EmployeeService(cls.db, cls.qr)
        cls.department_id = cls.employees.create_department(cls.admin_context, "Integration Department")
        cls.meals = MealService(cls.db)
        cls.approvals = ApprovalService(cls.db)
        with cls.db.transaction() as tx:
            cls.location_id = tx.insert(
                "INSERT INTO locations (code, name) VALUES (%s, %s)",
                ("INTEGRATION", "Integration Cafeteria"),
            )
            cls.scanner_id = tx.insert(
                "INSERT INTO scanner_devices (code, name, location_id) VALUES (%s, %s, %s)",
                ("TEST-SCANNER", "Integration Scanner", cls.location_id),
            )
            cls.meal_type_id = tx.insert(
                "INSERT INTO meal_types (code, name) VALUES (%s, %s)",
                ("TEST-LUNCH", "Integration Lunch"),
            )

    def employee(self):
        suffix = uuid4().hex[:12]
        registration = self.employees.register(
            self.admin_context,
            f"EMP-{suffix}",
            "Fictional Employee",
            f"employee-{suffix}@example.test",
            self.department_id,
            selfie_object_key=f"selfies/{suffix}.jpg",
        )
        preview = self.qr.email_queue.preview(self.admin_context, registration.email_id)
        return registration, preview.token

    def scan(self, token, request_id=None, quantity=1, authorization_id=None):
        from meal_management.models import ScanInput

        return ScanInput(
            request_id=request_id or uuid4(),
            token=token,
            meal_type_id=self.meal_type_id,
            scanner_code="TEST-SCANNER",
            quantity=quantity,
            authorization_id=authorization_id,
        )

    def master_approval(self, quantity=2, expires_at=None):
        credential = self.qr.issue_master(self.admin_context)
        request_id = uuid4()
        authorization_id = self.approvals.authorize(
            self.admin_context,
            request_id,
            credential.token,
            self.waiter_id,
            "TEST-SCANNER",
            self.meal_type_id,
            quantity,
            "Fictional Visitor",
            visitor_organization="Example Organization",
            visit_purpose="Integration meeting",
            expires_at=expires_at,
        )
        return credential, self.scan(credential.token, request_id, quantity, authorization_id)


class MealServicesMySQLTests(MealServicesFixture):
    def test_schema_has_exactly_two_qr_categories(self):
        kind = self.scalar(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (self.database_name, "qr_credentials", "kind"),
        )
        self.assertEqual(kind, "enum('EMPLOYEE','MASTER')")

    def test_registration_queues_encrypted_qr_and_resend_reuses_token(self):
        registration, token = self.employee()
        queue_id = self.qr.resend_employee(self.admin_context, registration.employee_id)
        preview = self.qr.email_queue.preview(self.admin_context, queue_id)
        self.assertEqual(preview.token, token)
        self.assertNotIn(token, repr(preview))
        self.assertIn("<svg", preview.qr_svg)
        qr_row = self.rows("SELECT * FROM qr_credentials WHERE id = %s", (registration.qr_id,))[0]
        email_row = self.rows("SELECT * FROM email_queue WHERE id = %s", (queue_id,))[0]
        self.assertNotIn(token.encode(), bytes(qr_row["token_ciphertext"]))
        self.assertNotIn(token.encode(), bytes(email_row["payload_ciphertext"]))
        self.assertEqual(email_row["status"], "QUEUED")
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM qr_credentials WHERE employee_id = %s", (registration.employee_id,)),
            1,
        )

    def test_employee_can_receive_multiple_deliberate_meals(self):
        registration, token = self.employee()
        first = self.meals.record(self.waiter_context, self.scan(token))
        second = self.meals.record(self.waiter_context, self.scan(token))
        self.assertTrue(first.approved)
        self.assertTrue(second.approved)
        self.assertNotEqual(first.serving_id, second.serving_id)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM meals m JOIN servings s ON s.id = m.serving_id WHERE s.employee_id = %s",
                (registration.employee_id,),
            ),
            2,
        )

    def test_concurrent_retries_create_one_serving_and_log_every_attempt(self):
        registration, token = self.employee()
        scan = self.scan(token)
        barrier = threading.Barrier(2)

        def record(context):
            barrier.wait(timeout=10)
            return self.meals.record(context, scan)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record, context) for context in (self.waiter_context, self.waiter_context_two)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(result.approved and not result.duplicate for result in results), 1)
        self.assertTrue(all(result.approved and result.code == "APPROVED" for result in results))
        self.assertEqual(results[0].serving_id, results[1].serving_id)
        self.assertEqual(results[0].meal_ids, results[1].meal_ids)
        self.assertTrue(any(result.duplicate for result in results))
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)),
            1,
        )
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM scan_attempts WHERE request_id = %s", (scan.request_id.bytes,)),
            2,
        )

    def test_reusing_request_identifier_with_changed_token_is_rejected(self):
        registration, token = self.employee()
        second_registration, second_token = self.employee()
        request_id = uuid4()
        original = self.meals.record(self.waiter_context, self.scan(token, request_id))
        changed = self.meals.record(self.waiter_context, self.scan(second_token, request_id))
        self.assertTrue(original.approved)
        self.assertFalse(changed.approved)
        self.assertIn("IDEMPOTENCY", changed.code)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (second_registration.employee_id,)),
            0,
        )

    def test_unknown_token_rejection_is_committed(self):
        from meal_management.security import generate_token

        scan = self.scan(generate_token())
        result = self.meals.record(self.waiter_context, scan)
        self.assertFalse(result.approved)
        attempt = self.rows("SELECT * FROM scan_attempts WHERE request_id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(attempt["outcome"], "REJECTED")
        self.assertEqual(attempt["rejection_code"], result.code)
        self.assertEqual(attempt["scanner_id"], self.scanner_id)
        self.assertEqual(attempt["location_id"], self.location_id)
        self.assertIsNotNone(attempt["completed_at"])

    def test_inactive_employee_cannot_receive_meal(self):
        registration, token = self.employee()
        self.employees.set_active(self.admin_context, registration.employee_id, False)
        result = self.meals.record(self.waiter_context, self.scan(token))
        self.assertFalse(result.approved)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)),
            0,
        )

    def test_replacement_revokes_old_qr_and_preserves_meal_history(self):
        registration, token = self.employee()
        original = self.meals.record(self.waiter_context, self.scan(token))
        replacement = self.qr.replace_employee(self.admin_context, registration.employee_id)
        revoked_result = self.meals.record(self.waiter_context, self.scan(token))
        replacement_result = self.meals.record(self.waiter_context, self.scan(replacement.token))
        self.assertTrue(original.approved)
        self.assertFalse(revoked_result.approved)
        self.assertTrue(replacement_result.approved)
        self.assertNotEqual(token, replacement.token)
        old = self.rows("SELECT * FROM qr_credentials WHERE id = %s", (registration.qr_id,))[0]
        self.assertIsNotNone(old["revoked_at"])
        self.assertIsNone(old["token_ciphertext"])
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)),
            2,
        )

    def test_master_serving_records_each_unit_and_both_staff_identities(self):
        credential, scan = self.master_approval(quantity=3)
        result = self.meals.record(self.waiter_context, scan)
        self.assertTrue(result.approved)
        self.assertEqual(len(result.meal_ids), 3)
        row = self.rows(
            "SELECT s.quantity, s.waiter_id, s.location_id, a.authorized_by, a.visitor_name "
            "FROM servings s JOIN visitor_authorizations a ON a.id = s.authorization_id WHERE s.id = %s",
            (result.serving_id,),
        )[0]
        self.assertEqual(row["quantity"], 3)
        self.assertEqual(row["waiter_id"], self.waiter_id)
        self.assertEqual(row["authorized_by"], self.admin_id)
        self.assertEqual(row["visitor_name"], "Fictional Visitor")
        self.assertEqual(row["location_id"], self.location_id)

    def test_master_without_visitor_details_or_legacy_authorization_is_rejected(self):
        credential = self.qr.issue_master(self.admin_context)
        result = self.meals.record(self.waiter_context, self.scan(credential.token, quantity=2))
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "VISITOR_DETAILS_REQUIRED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 0)

    def test_revoked_authorization_is_rejected(self):
        credential, scan = self.master_approval()
        self.approvals.revoke(self.admin_context, scan.authorization_id)
        result = self.meals.record(self.waiter_context, scan)
        self.assertFalse(result.approved)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 0)

    def test_authorization_cannot_be_reused_for_a_new_request(self):
        credential, scan = self.master_approval()
        original = self.meals.record(self.waiter_context, scan)
        other = self.scan(credential.token, quantity=scan.quantity, authorization_id=scan.authorization_id)
        result = self.meals.record(self.waiter_context, other)
        self.assertTrue(original.approved)
        self.assertFalse(result.approved)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_authorization_cannot_be_used_by_another_waiter(self):
        credential, scan = self.master_approval()
        password = secrets.token_urlsafe(32)
        email = f"other-waiter-{uuid4().hex[:12]}@example.test"
        self.staff.create_staff(self.admin_context, "Other Waiter", email, password, {"WAITER"})
        other_context = self.staff.authenticate(email, password)
        unauthorized = self.meals.record(other_context, scan)
        authorized = self.meals.record(self.waiter_context, scan)
        self.assertFalse(unauthorized.approved)
        self.assertTrue(authorized.approved)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_authorization_quantity_cannot_be_increased(self):
        credential, scan = self.master_approval(quantity=2)
        changed = self.scan(credential.token, scan.request_id, 3, scan.authorization_id)
        unauthorized = self.meals.record(self.waiter_context, changed)
        authorized = self.meals.record(self.waiter_context, scan)
        self.assertFalse(unauthorized.approved)
        self.assertTrue(authorized.approved)
        self.assertEqual(len(authorized.meal_ids), 2)

    def test_corrected_admin_proof_succeeds_after_rejected_proof_for_same_request(self):
        for proof in ("missing", "wrong"):
            with self.subTest(proof=proof):
                credential, prepared_scan = self.master_approval(quantity=2)
                wrong_id = None if proof == "missing" else prepared_scan.authorization_id + 1000000
                incorrect_scan = self.scan(
                    credential.token, prepared_scan.request_id, prepared_scan.quantity, wrong_id
                )
                rejected = self.meals.record(self.waiter_context, incorrect_scan)
                self.assertFalse(rejected.approved)
                self.assertEqual(rejected.code, "AUTHORIZATION_SCOPE_MISMATCH")
                self.assertEqual(
                    self.scalar("SELECT status FROM serving_requests WHERE id = %s", (prepared_scan.request_id.bytes,)),
                    "PENDING",
                )
                corrected = self.meals.record(self.waiter_context, prepared_scan)
                self.assertTrue(corrected.approved)
                self.assertEqual(len(corrected.meal_ids), 2)
                self.assertEqual(
                    self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (prepared_scan.request_id.bytes,)),
                    1,
                )
                attempts = self.rows(
                    "SELECT outcome FROM scan_attempts WHERE request_id = %s ORDER BY id",
                    (prepared_scan.request_id.bytes,),
                )
                self.assertEqual([attempt["outcome"] for attempt in attempts], ["REJECTED", "SUCCESS"])

    def test_email_change_cancels_stale_delivery_and_resends_to_current_address(self):
        registration, token = self.employee()
        new_email = f"changed-{uuid4().hex[:12]}@example.test"
        self.employees.update(self.admin_context, registration.employee_id, email=new_email)
        old_email = self.rows("SELECT status, payload_ciphertext FROM email_queue WHERE id = %s", (registration.email_id,))[0]
        self.assertEqual(old_email["status"], "CANCELLED")
        self.assertIsNone(old_email["payload_ciphertext"])
        email_id = self.qr.resend_employee(self.admin_context, registration.employee_id)
        preview = self.qr.email_queue.preview(self.admin_context, email_id)
        self.assertEqual(preview.recipient_email, new_email)
        self.assertEqual(preview.token, token)

    def test_waiter_cannot_create_admin_authorization(self):
        from meal_management.errors import DomainError

        credential = self.qr.issue_master(self.admin_context)
        with self.assertRaises(DomainError):
            self.approvals.authorize(
                self.waiter_context, uuid4(), credential.token, self.waiter_id,
                "TEST-SCANNER", self.meal_type_id, 1, "Fictional Visitor",
            )

    def test_expired_credential_is_rejected(self):
        from meal_management.security import generate_token, token_digest

        token = generate_token()
        with self.db.transaction() as tx:
            now = tx.now()
            qr_id = tx.insert(
                "INSERT INTO qr_credentials "
                "(kind, employee_id, token_hash, token_ciphertext, issued_by, issued_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    "MASTER", None, token_digest(token), self.vault.encrypt(token), self.admin_id,
                    now - timedelta(days=2), now - timedelta(days=1),
                ),
            )
        result = self.meals.record(self.waiter_context, self.scan(token))
        self.assertFalse(result.approved)
        self.assertIn("EXPIRED", result.code)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (qr_id,)), 0)

    def test_expired_authorization_is_rejected_when_scanned(self):
        from meal_management.meals import scan_fingerprint
        from meal_management.security import token_digest

        credential = self.qr.issue_master(self.admin_context)
        identifier = uuid4()
        fingerprint = scan_fingerprint(
            token_digest(credential.token), self.waiter_id, self.scanner_id,
            self.location_id, self.meal_type_id, 2,
        )
        with self.db.transaction() as tx:
            now = tx.now()
            authorized_at = now - timedelta(minutes=2)
            tx.insert(
                "INSERT INTO serving_requests (id, payload_hash, created_at) VALUES (%s, %s, %s)",
                (identifier.bytes, fingerprint, authorized_at),
            )
            authorization_id = tx.insert(
                "INSERT INTO visitor_authorizations "
                "(request_id, qr_id, meal_type_id, location_id, quantity, waiter_id, scanner_id, "
                "visitor_name, authorized_by, authorized_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    identifier.bytes, credential.qr_id, self.meal_type_id, self.location_id,
                    2, self.waiter_id, self.scanner_id, "Expired Approval Visitor",
                    self.admin_id, authorized_at, now - timedelta(minutes=1),
                ),
            )
        result = self.meals.record(
            self.waiter_context, self.scan(credential.token, identifier, 2, authorization_id)
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "AUTHORIZATION_EXPIRED")

    def test_partial_meal_batch_rolls_back_and_preserves_scan_receipt(self):
        credential, scan = self.master_approval(quantity=2)
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "CREATE TRIGGER integration_fail_second_meal BEFORE INSERT ON meals FOR EACH ROW "
                "BEGIN IF NEW.unit_number = 2 THEN "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Intentional integration failure'; "
                "END IF; END"
            )
        finally:
            cursor.close()
        try:
            result = self.meals.record(self.waiter_context, scan)
        finally:
            cursor = self.admin_connection.cursor()
            try:
                cursor.execute("DROP TRIGGER integration_fail_second_meal")
            finally:
                cursor.close()
        self.assertFalse(result.approved)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 0)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM meals m JOIN servings s ON s.id = m.serving_id WHERE s.qr_id = %s",
                (credential.qr_id,),
            ),
            0,
        )
        attempt = self.rows("SELECT outcome, rejection_code FROM scan_attempts WHERE request_id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(attempt["outcome"], "REJECTED")
        self.assertEqual(attempt["rejection_code"], "PROCESSING_INTERRUPTED")
        retry = self.meals.record(self.waiter_context, scan)
        self.assertTrue(retry.approved)
        self.assertEqual(len(retry.meal_ids), 2)

    def test_employee_history_uses_utc_and_half_open_date_ranges(self):
        from meal_management.reports import ReportService

        registration, token = self.employee()
        result = self.meals.record(self.waiter_context, self.scan(token))
        self.assertTrue(result.approved)
        served_at = self.scalar("SELECT served_at FROM meals WHERE id = %s", (result.meal_ids[0],))
        boundary = served_at.replace(tzinfo=timezone.utc)
        reports = ReportService(self.db)
        included = reports.employee_history(
            self.admin_context, registration.employee_id, boundary, boundary + timedelta(microseconds=1)
        )
        excluded = reports.employee_history(
            self.admin_context, registration.employee_id, boundary - timedelta(seconds=1), boundary
        )
        self.assertEqual([row["meal_id"] for row in included], list(result.meal_ids))
        self.assertEqual(included[0]["served_at"], boundary)
        self.assertEqual(included[0]["waiter_id"], self.waiter_id)
        self.assertEqual(included[0]["location_id"], self.location_id)
        self.assertEqual(excluded, [])

    def test_date_range_report_counts_meals_and_servings_separately(self):
        from meal_management.reports import ReportService

        credential, scan = self.master_approval(quantity=3)
        result = self.meals.record(self.waiter_context, scan)
        self.assertTrue(result.approved)
        served_at = self.scalar("SELECT served_at FROM meals WHERE id = %s", (result.meal_ids[0],))
        start = served_at.replace(tzinfo=timezone.utc)
        end = start + timedelta(microseconds=1)
        report = ReportService(self.db).meal_report(self.admin_context, start, end)
        self.assertEqual(report["totals"]["meal_count"], 3)
        self.assertEqual(report["totals"]["serving_count"], 1)
        self.assertEqual(report["totals"]["visitor_meals"], 3)
        self.assertEqual(report["totals"]["employee_meals"], 0)
        self.assertEqual(report["by_authorizing_admin"][0]["admin_id"], self.admin_id)

    def test_successful_retry_returns_original_approval_after_qr_revocation(self):
        registration, token = self.employee()
        scan = self.scan(token)
        original = self.meals.record(self.waiter_context, scan)
        self.qr.revoke(self.admin_context, registration.qr_id, "Integration replacement")
        repeated = self.meals.record(self.waiter_context, scan)
        self.assertTrue(original.approved)
        self.assertTrue(repeated.approved)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(repeated.code, original.code)
        self.assertEqual(repeated.serving_id, original.serving_id)
        self.assertEqual(repeated.meal_ids, original.meal_ids)

    def test_concurrent_master_retries_return_one_original_batch(self):
        credential, scan = self.master_approval(quantity=3)
        barrier = threading.Barrier(2)

        def record(context):
            barrier.wait(timeout=10)
            return self.meals.record(context, scan)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record, context) for context in (self.waiter_context, self.waiter_context_two)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(result.approved for result in results))
        self.assertEqual(sum(not result.duplicate for result in results), 1)
        self.assertEqual(results[0].meal_ids, results[1].meal_ids)
        self.assertEqual(len(results[0].meal_ids), 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_concurrent_deliberate_employee_servings_create_distinct_meals(self):
        registration, token = self.employee()
        barrier = threading.Barrier(2)
        scans = [self.scan(token), self.scan(token)]

        def record(context, scan):
            barrier.wait(timeout=10)
            return self.meals.record(context, scan)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(record, context, scan)
                for context, scan in zip((self.waiter_context, self.waiter_context_two), scans)
            ]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(result.approved and not result.duplicate for result in results))
        self.assertNotEqual(results[0].meal_ids, results[1].meal_ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 2)

    def test_rejected_request_replay_is_bound_to_original_proof(self):
        registration, token = self.employee()
        scan = self.scan(token, authorization_id=99999999)
        original = self.meals.record(self.waiter_context, scan)
        repeated = self.meals.record(self.waiter_context, scan)
        changed = self.meals.record(self.waiter_context, self.scan(token, scan.request_id))
        self.assertEqual(original.code, "AUTHORIZATION_NOT_APPLICABLE")
        self.assertEqual(repeated.code, original.code)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(changed.code, "IDEMPOTENCY_KEY_REUSED")
        self.assertFalse(changed.duplicate)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 0)

    def test_completed_serving_cannot_be_replayed_by_another_waiter(self):
        registration, token = self.employee()
        scan = self.scan(token)
        original = self.meals.record(self.waiter_context, scan)
        password = secrets.token_urlsafe(32)
        email = f"replay-waiter-{uuid4().hex[:12]}@example.test"
        self.staff.create_staff(self.admin_context, "Replay Waiter", email, password, {"WAITER"})
        other_context = self.staff.authenticate(email, password)
        unauthorized = self.meals.record(other_context, scan)
        self.assertTrue(original.approved)
        self.assertFalse(unauthorized.approved)
        self.assertEqual(unauthorized.code, "IDEMPOTENCY_KEY_REUSED")
        self.assertIsNone(unauthorized.serving_id)

    def test_query_pages_match_records_and_enforce_waiter_scope(self):
        from meal_management.errors import DomainError
        from meal_management.queries import QueryService

        registration, token = self.employee()
        scan = self.scan(token)
        result = self.meals.record(self.waiter_context, scan)
        served_at = self.scalar("SELECT served_at FROM meals WHERE id = %s", (result.meal_ids[0],))
        start = served_at.replace(tzinfo=timezone.utc)
        queries = QueryService(self.db)
        page = queries.meal_history(
            self.admin_context, start, start + timedelta(microseconds=1), employee_id=registration.employee_id
        )
        self.assertEqual(page["items"][0]["id"], result.meal_ids[0])
        self.assertEqual(page["items"][0]["request_id"], str(scan.request_id))
        self.assertEqual(page["items"][0]["employee_id"], registration.employee_id)
        emails = queries.email_status(self.admin_context, employee_id=registration.employee_id)
        self.assertEqual(emails["items"][0]["status"], "QUEUED")
        self.assertNotIn("payload_ciphertext", emails["items"][0])
        self.assertIn("roles", queries.staff(self.admin_context)["items"][0])
        with self.assertRaises(DomainError):
            queries.list_employees(self.waiter_context)
        for authorization in queries.visitor_authorizations(self.waiter_context, status="all")["items"]:
            self.assertEqual(authorization["waiter_id"], self.waiter_id)

    def test_logout_revokes_an_already_expired_session_idempotently(self):
        from meal_management.errors import DomainError
        from meal_management.security import token_digest

        password = secrets.token_urlsafe(32)
        email = f"logout-waiter-{uuid4().hex[:12]}@example.test"
        self.staff.create_staff(self.admin_context, "Logout Waiter", email, password, {"WAITER"})
        context = self.staff.authenticate(email, password)
        with self.db.transaction() as tx:
            now = tx.now()
            tx.execute(
                "UPDATE staff_sessions SET created_at = %s, expires_at = %s, last_seen_at = %s WHERE token_hash = %s",
                (now - timedelta(hours=2), now - timedelta(hours=1), now - timedelta(hours=2), token_digest(context.session_token)),
            )
        self.staff.logout(context)
        self.staff.logout(context)
        self.assertIsNotNone(self.scalar("SELECT revoked_at FROM staff_sessions WHERE token_hash = %s", (token_digest(context.session_token),)))
        with self.assertRaises(DomainError):
            self.staff.current(context)

    def test_terminal_request_proof_cannot_be_rewritten(self):
        import mysql.connector

        registration, token = self.employee()
        scan = self.scan(token)
        original = self.meals.record(self.waiter_context, scan)
        self.assertTrue(original.approved)
        with self.assertRaises(mysql.connector.Error):
            with self.db.transaction() as tx:
                tx.execute("UPDATE serving_requests SET scan_authorization_id = %s WHERE id = %s", (999, scan.request_id.bytes))
        repeated = self.meals.record(self.waiter_context, scan)
        self.assertTrue(repeated.approved)
        self.assertEqual(repeated.meal_ids, original.meal_ids)

    def test_terminal_retry_after_scanner_relocation_preserves_original_result(self):
        registration, token = self.employee()
        code = "MOVE-" + uuid4().hex[:12]
        with self.db.transaction() as tx:
            scanner_id = tx.insert(
                "INSERT INTO scanner_devices (code, name, location_id) VALUES (%s, %s, %s)",
                (code, "Relocatable Scanner", self.location_id),
            )
            moved_location = tx.insert(
                "INSERT INTO locations (code, name) VALUES (%s, %s)",
                ("MOVE-" + uuid4().hex[:12], "Relocated Cafeteria"),
            )
        scan = replace(self.scan(token), scanner_code=code)
        original = self.meals.record(self.waiter_context, scan)
        with self.db.transaction() as tx:
            tx.execute("UPDATE scanner_devices SET location_id = %s WHERE id = %s", (moved_location, scanner_id))
        repeated = self.meals.record(self.waiter_context, scan)
        self.assertTrue(original.approved)
        self.assertTrue(repeated.approved)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(repeated.meal_ids, original.meal_ids)
        self.assertEqual(self.scalar("SELECT location_id FROM servings WHERE id = %s", (original.serving_id,)), self.location_id)
        attempts = self.rows("SELECT location_id FROM scan_attempts WHERE request_id = %s ORDER BY id", (scan.request_id.bytes,))
        self.assertEqual([row["location_id"] for row in attempts], [self.location_id, moved_location])

    def test_pending_master_authorization_rejects_scanner_relocation(self):
        credential = self.qr.issue_master(self.admin_context)
        code = "MOVE-MASTER-" + uuid4().hex[:12]
        with self.db.transaction() as tx:
            scanner_id = tx.insert(
                "INSERT INTO scanner_devices (code, name, location_id) VALUES (%s, %s, %s)",
                (code, "Approval Scanner", self.location_id),
            )
            moved_location = tx.insert(
                "INSERT INTO locations (code, name) VALUES (%s, %s)",
                ("MASTER-" + uuid4().hex[:12], "Changed Approval Location"),
            )
        identifier = uuid4()
        authorization_id = self.approvals.authorize(
            self.admin_context, identifier, credential.token, self.waiter_id, code,
            self.meal_type_id, 2, "Relocation Visitor",
        )
        with self.db.transaction() as tx:
            tx.execute("UPDATE scanner_devices SET location_id = %s WHERE id = %s", (moved_location, scanner_id))
        scan = replace(self.scan(credential.token, identifier, 2, authorization_id), scanner_code=code)
        result = self.meals.record(self.waiter_context, scan)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (identifier.bytes,)), 0)

    def test_employee_issuance_does_not_replace_concurrently_issued_qr(self):
        from meal_management.errors import DomainError

        registration, token = self.employee()
        self.qr.revoke(self.admin_context, registration.qr_id, "Prepare concurrent issuance")
        barrier = threading.Barrier(2)

        def issue():
            barrier.wait(timeout=10)
            try:
                return self.qr.issue_employee(self.admin_context, registration.employee_id)
            except DomainError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(issue) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(result == "EMPLOYEE_QR_ALREADY_EXISTS" for result in results), 1)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM qr_credentials WHERE employee_id = %s AND revoked_at IS NULL",
            (registration.employee_id,),
        ), 1)


if __name__ == "__main__":
    unittest.main()
