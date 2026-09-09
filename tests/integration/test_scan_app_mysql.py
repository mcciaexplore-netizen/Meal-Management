import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class ScanAppMySQLTests(MealServicesFixture):
    def visitor(self):
        return {
            "company_name": " Example Company ", "name": " Fictional Visitor ",
            "email": " Visitor@Example.TEST ", "phone": " +91 98765 43210 ",
        }

    def test_master_read_waits_then_records_one_direct_visitor_meal_with_committed_details(self):
        from meal_management.scan_receipts import ScanReceiptService
        from meal_management.security import payload_digest
        from meal_management.meals import normalize_visitor_details

        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        expected = {"kind": "MASTER", "next": "VISITOR_DETAILS", "request_id": str(scan.request_id)}
        self.assertEqual(self.meals.read(self.waiter_context, scan), expected)
        self.assertEqual(self.meals.read(self.waiter_context, scan), expected)
        request = self.rows("SELECT * FROM serving_requests WHERE id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(request["status"], "PENDING")
        self.assertIsNone(request["scan_bound_at"])
        self.assertIsNone(request["scan_visitor_hash"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM meals m JOIN servings s ON s.id = m.serving_id WHERE s.qr_id = %s", (credential.qr_id,)), 0)
        attempts = self.rows("SELECT outcome, completed_at, serving_id, rejection_code FROM scan_attempts WHERE request_id = %s", (scan.request_id.bytes,))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt["outcome"] == "AWAITING_DETAILS" and attempt["completed_at"] for attempt in attempts))
        self.assertTrue(all(attempt["serving_id"] is None and attempt["rejection_code"] is None for attempt in attempts))
        self.assertEqual(ScanReceiptService(self.db).result(self.waiter_context, scan.request_id)["code"], "PROCESSING_UNCONFIRMED")
        result = self.meals.record(self.waiter_context, replace(scan, visitor_details=self.visitor()))
        self.assertTrue(result.approved)
        self.assertEqual(len(result.meal_ids), 1)
        serving = self.rows("SELECT * FROM servings WHERE id = %s", (result.serving_id,))[0]
        self.assertIsNone(serving["authorization_id"])
        self.assertEqual(serving["quantity"], 1)
        normalized = normalize_visitor_details(self.visitor())
        for column, key in (("visitor_company_name", "company_name"), ("visitor_name", "name"), ("visitor_email", "email"), ("visitor_phone", "phone")):
            self.assertEqual(serving[column], normalized[key])
        bound = self.rows("SELECT scan_visitor_hash, scan_bound_at FROM serving_requests WHERE id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(bytes(bound["scan_visitor_hash"]), payload_digest(normalized))
        self.assertIsNotNone(bound["scan_bound_at"])
        self.assertEqual(ScanReceiptService(self.db).get(self.waiter_context, scan.request_id)["visitor_name"], "Fictional Visitor")
        recovered = self.meals.read(self.waiter_context, scan)
        self.assertTrue(recovered.approved)
        self.assertTrue(recovered.duplicate)
        self.assertEqual(recovered.meal_ids, result.meal_ids)

    def test_direct_master_simultaneous_final_requests_commit_one_meal(self):
        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        self.meals.read(self.waiter_context, scan)
        final = replace(scan, visitor_details=self.visitor())
        barrier = threading.Barrier(2)

        def record(context):
            barrier.wait(timeout=10)
            return self.meals.record(context, final)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record, context) for context in (self.waiter_context, self.waiter_context_two)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(result.approved for result in results))
        self.assertEqual(sum(not result.duplicate for result in results), 1)
        self.assertEqual(results[0].meal_ids, results[1].meal_ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (scan.request_id.bytes,)), 1)

    def test_simultaneous_different_visitor_submissions_bind_only_the_winner(self):
        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        self.meals.read(self.waiter_context, scan)
        first_details = self.visitor()
        second_details = {**self.visitor(), "name": "Second Fictional Visitor"}
        barrier = threading.Barrier(2)

        def record(context, details):
            barrier.wait(timeout=10)
            return details, self.meals.record(context, replace(scan, visitor_details=details))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(record, self.waiter_context, first_details),
                pool.submit(record, self.waiter_context_two, second_details),
            ]
            outcomes = [future.result(timeout=30) for future in futures]
        successful = [(details, result) for details, result in outcomes if result.approved]
        rejected = [result for _, result in outcomes if not result.approved]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].code, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.scalar("SELECT visitor_name FROM servings WHERE request_id = %s", (scan.request_id.bytes,)), successful[0][0]["name"].strip())
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM meals m JOIN servings s ON s.id = m.serving_id WHERE s.request_id = %s", (scan.request_id.bytes,)), 1)

    def test_failed_direct_meal_insert_rolls_back_private_visitor_fields_and_binding(self):
        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        self.meals.read(self.waiter_context, scan)
        final = replace(scan, visitor_details=self.visitor())
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "CREATE TRIGGER integration_fail_direct_meal BEFORE INSERT ON meals FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Intentional direct meal failure'"
            )
        finally:
            cursor.close()
        try:
            result = self.meals.record(self.waiter_context, final)
        finally:
            cursor = self.admin_connection.cursor()
            try:
                cursor.execute("DROP TRIGGER integration_fail_direct_meal")
            finally:
                cursor.close()
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE request_id = %s", (scan.request_id.bytes,)), 0)
        request = self.rows("SELECT status, scan_visitor_hash, scan_bound_at FROM serving_requests WHERE id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(request["status"], "PENDING")
        self.assertIsNone(request["scan_visitor_hash"])
        self.assertIsNone(request["scan_bound_at"])
        attempts = self.rows("SELECT outcome, rejection_code FROM scan_attempts WHERE request_id = %s ORDER BY id", (scan.request_id.bytes,))
        self.assertEqual(attempts[-1]["rejection_code"], "PROCESSING_INTERRUPTED")
        retried = self.meals.record(self.waiter_context, final)
        self.assertTrue(retried.approved)
        self.assertEqual(len(retried.meal_ids), 1)

    def test_committed_direct_visitor_fields_and_request_hash_are_immutable(self):
        import mysql.connector

        credential = self.qr.issue_master(self.admin_context)
        scan = replace(self.scan(credential.token), visitor_details=self.visitor())
        result = self.meals.record(self.waiter_context, scan)
        self.assertTrue(result.approved)
        for sql, parameters in (
            ("UPDATE servings SET visitor_name = %s WHERE id = %s", ("Rewritten Visitor", result.serving_id)),
            ("UPDATE serving_requests SET scan_visitor_hash = %s WHERE id = %s", (b"x" * 32, scan.request_id.bytes)),
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(mysql.connector.Error):
                    with self.db.transaction() as tx:
                        tx.execute(sql, parameters)
        self.assertEqual(self.scalar("SELECT visitor_name FROM servings WHERE id = %s", (result.serving_id,)), "Fictional Visitor")

    def test_changed_visitor_fields_cannot_reuse_committed_request(self):
        credential = self.qr.issue_master(self.admin_context)
        scan = replace(self.scan(credential.token), visitor_details=self.visitor())
        original = self.meals.record(self.admin_context, scan)
        self.assertTrue(original.approved)
        changes = {"company_name": "Different Company", "name": "Different Name", "email": "another@example.test", "phone": "+919876543211"}
        for key, value in changes.items():
            with self.subTest(key=key):
                result = self.meals.record(self.admin_context, replace(scan, visitor_details={**self.visitor(), key: value}))
                self.assertFalse(result.approved)
                self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        absent = self.meals.record(self.admin_context, replace(scan, visitor_details=None))
        self.assertEqual(absent.code, "IDEMPOTENCY_KEY_REUSED")
        exact = self.meals.record(self.admin_context, scan)
        self.assertTrue(exact.approved)
        self.assertTrue(exact.duplicate)
        self.assertEqual(exact.meal_ids, original.meal_ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)), 1)

    def test_pending_master_is_bound_to_caller_and_missing_or_invalid_details_do_not_poison_it(self):
        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        self.meals.read(self.waiter_context, scan)
        foreign = self.meals.read(self.admin_context, scan)
        self.assertEqual(foreign.code, "IDEMPOTENCY_KEY_REUSED")
        missing = self.meals.record(self.waiter_context, scan)
        self.assertEqual(missing.code, "VISITOR_DETAILS_REQUIRED")
        invalid = self.meals.record(self.waiter_context, replace(scan, visitor_details={"name": "Only Name"}))
        self.assertEqual(invalid.code, "REQUEST_MISMATCH")
        row = self.rows("SELECT status, scan_bound_at FROM serving_requests WHERE id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["scan_bound_at"])
        corrected = self.meals.record(self.waiter_context, replace(scan, visitor_details=self.visitor()))
        self.assertTrue(corrected.approved)

    def test_malformed_retry_preserves_original_commit_recovery_and_does_not_grant_foreign_ownership(self):
        from meal_management.errors import DomainError
        from meal_management.scan_receipts import ScanReceiptService

        registration, token = self.employee()
        scan = self.scan(token)
        original = self.meals.read(self.waiter_context, scan)
        self.assertTrue(original.approved)
        malformed = replace(scan, token="https://not-a-meal-qr.example.test")
        for context in (self.waiter_context, self.admin_context):
            with self.subTest(context=context):
                retry = self.meals.read(context, malformed)
                self.assertFalse(retry.approved)
                self.assertEqual(retry.code, "REQUEST_MISMATCH")
                self.assertEqual(retry.request_id, str(scan.request_id))
        recovered = ScanReceiptService(self.db).result(self.waiter_context, scan.request_id)
        self.assertTrue(recovered["approved"])
        self.assertEqual(recovered["meal_ids"], list(original.meal_ids))
        with self.assertRaises(DomainError) as caught:
            ScanReceiptService(self.db).result(self.admin_context, scan.request_id)
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 1)
        last_attempts = self.rows("SELECT request_id, payload_hash, token_hash, rejection_code FROM scan_attempts ORDER BY id DESC LIMIT 2")
        self.assertTrue(all(row["rejection_code"] == "INVALID_QR" for row in last_attempts))
        self.assertTrue(all(row["request_id"] is None and row["payload_hash"] is None and row["token_hash"] is None for row in last_attempts))

    def test_malformed_master_retry_cannot_finalize_pending_visitor_details_request(self):
        from meal_management.scan_receipts import ScanReceiptService

        credential = self.qr.issue_master(self.admin_context)
        scan = self.scan(credential.token)
        self.meals.read(self.waiter_context, scan)
        retry = self.meals.read(self.waiter_context, replace(scan, token="malformed-qr"))
        self.assertEqual(retry.code, "REQUEST_MISMATCH")
        self.assertEqual(ScanReceiptService(self.db).result(self.waiter_context, scan.request_id)["code"], "PROCESSING_UNCONFIRMED")
        pending = self.rows("SELECT status, scan_bound_at, scan_visitor_hash FROM serving_requests WHERE id = %s", (scan.request_id.bytes,))[0]
        self.assertEqual(pending["status"], "PENDING")
        self.assertIsNone(pending["scan_bound_at"])
        self.assertIsNone(pending["scan_visitor_hash"])
        final = self.meals.record(self.waiter_context, replace(scan, visitor_details=self.visitor()))
        self.assertTrue(final.approved)
        self.assertEqual(len(final.meal_ids), 1)

    def test_employee_read_reuses_qr_but_only_explicit_new_request_creates_new_meal(self):
        registration, token = self.employee()
        scan = self.scan(token)
        first = self.meals.read(self.admin_context, scan)
        retry = self.meals.read(self.admin_context, scan)
        deliberate = self.meals.read(self.admin_context, self.scan(token))
        self.assertTrue(first.approved and retry.approved and deliberate.approved)
        self.assertEqual(first.meal_ids, retry.meal_ids)
        self.assertTrue(retry.duplicate)
        self.assertNotEqual(first.meal_ids, deliberate.meal_ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM servings WHERE employee_id = %s", (registration.employee_id,)), 2)

    def test_current_invalid_credentials_are_rejected_before_visitor_form(self):
        for condition in ("inactive", "revoked"):
            with self.subTest(condition=condition):
                registration, token = self.employee()
                if condition == "inactive":
                    self.employees.set_active(self.admin_context, registration.employee_id, False)
                else:
                    self.qr.revoke(self.admin_context, registration.qr_id, "Read validation fixture")
                result = self.meals.read(self.waiter_context, self.scan(token))
                self.assertFalse(result.approved)
                self.assertEqual(result.code, "EMPLOYEE_INACTIVE" if condition == "inactive" else "QR_REVOKED")
        master = self.qr.issue_master(self.admin_context)
        self.qr.revoke(self.admin_context, master.qr_id, "Revoked master read fixture")
        self.assertEqual(self.meals.read(self.waiter_context, self.scan(master.token)).code, "QR_REVOKED")

    def test_direct_and_legacy_master_meals_are_both_visible_in_reports(self):
        from meal_management.queries import QueryService
        from meal_management.reports import ReportService

        credential = self.qr.issue_master(self.admin_context)
        direct = self.meals.record(self.waiter_context, replace(self.scan(credential.token), visitor_details=self.visitor()))
        _, legacy_scan = self.master_approval(quantity=2)
        legacy = self.meals.record(self.waiter_context, legacy_scan)
        self.assertTrue(direct.approved and legacy.approved)
        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = start + timedelta(days=2)
        items = QueryService(self.db).meal_history(self.admin_context, start, end, limit=1000)["items"]
        direct_rows = [row for row in items if row["serving_id"] == direct.serving_id]
        legacy_rows = [row for row in items if row["serving_id"] == legacy.serving_id]
        self.assertEqual(len(direct_rows), 1)
        self.assertEqual(len(legacy_rows), 2)
        self.assertEqual(direct_rows[0]["visitor_name"], "Fictional Visitor")
        self.assertEqual(direct_rows[0]["visitor_email"], "visitor@example.test")
        self.assertEqual(legacy_rows[0]["visitor_name"], "Fictional Visitor")
        self.assertIsNone(legacy_rows[0]["visitor_email"])
        report = ReportService(self.db).meal_report(self.admin_context, start, end)
        self.assertGreaterEqual(report["totals"]["visitor_meals"], 3)

    def test_database_rejects_incomplete_or_multi_quantity_direct_master_serving(self):
        import mysql.connector

        from meal_management.meals import reserve_request, scan_fingerprint
        from meal_management.security import token_digest

        credential = self.qr.issue_master(self.admin_context)
        for quantity, name in ((2, "Fictional Visitor"), (1, None), (1, " ")):
            with self.subTest(quantity=quantity, name=name):
                identifier = uuid4()
                with self.assertRaises(mysql.connector.Error):
                    with self.db.transaction() as tx:
                        reserve_request(tx, identifier, scan_fingerprint(
                            token_digest(credential.token), self.waiter_id, self.scanner_id,
                            self.location_id, self.meal_type_id, quantity,
                        ))
                        tx.insert(
                            "INSERT INTO servings (request_id, qr_id, kind, employee_id, meal_type_id, quantity, "
                            "waiter_id, scanner_id, location_id, authorization_id, visitor_company_name, visitor_name, visitor_email, visitor_phone) "
                            "VALUES (%s, %s, 'MASTER', NULL, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)",
                            (identifier.bytes, credential.qr_id, self.meal_type_id, quantity, self.waiter_id,
                             self.scanner_id, self.location_id, "Example Company", name, "visitor@example.test", "+919876543210"),
                        )
