import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from mysql_support import schema_statements
from test_mysql_services import MealServicesFixture


class EmployeeEmailPolicyMySQLTests(MealServicesFixture):
    def inputs(self, count=2):
        prefix = uuid4().hex[:12]
        return [{
            "employee_code": "BULK-" + prefix + "-" + str(index),
            "full_name": "Fictional Bulk Employee " + str(index),
            "email": "bulk-" + prefix + "-" + str(index) + "@example.test",
            "department_id": self.department_id,
        } for index in range(count)]

    def batch(self):
        return self.employees.register_bulk(self.admin_context, uuid4(), self.inputs())

    def query(self):
        from meal_management.queries import QueryService

        return QueryService(self.db)

    def test_single_registration_is_draft_and_hidden_from_bulk_queue(self):
        registration, _ = self.employee()
        queue = self.qr.email_queue
        status = queue.status(self.admin_context, registration.email_id, employee_id=registration.employee_id)
        self.assertEqual(status["status"], "DRAFT")
        self.assertEqual(status["delivery_mode"], "SINGLE")
        self.assertIsNone(status["approved_at"])
        self.assertEqual(self.query().email_status(self.admin_context, employee_id=registration.employee_id, delivery_scope="bulk")["items"], [])
        self.assertEqual(self.query().email_status(self.admin_context, employee_id=registration.employee_id)["items"][0]["id"], registration.email_id)

    def test_bulk_registration_creates_pending_email_and_explicit_approval_is_idempotent(self):
        batch = self.batch()
        identifiers = [row["email_id"] for row in batch["employees"]]
        rows = self.query().email_status(self.admin_context, delivery_scope="bulk", bulk_batch_id=batch["batch_id"])["items"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "PENDING_APPROVAL" and row["delivery_mode"] == "BULK" for row in rows))
        queue = self.qr.email_queue
        self.assertEqual(queue.approve_bulk(self.admin_context, identifiers)["approved_count"], 2)
        before = self.rows("SELECT id, approved_by_staff_id, approved_at FROM email_queue WHERE bulk_batch_id = %s ORDER BY id", (batch["batch_id"],))
        self.assertTrue(all(row["approved_by_staff_id"] == self.admin_id and row["approved_at"] is not None for row in before))
        self.assertEqual(queue.approve_bulk(self.admin_context, identifiers)["approved_count"], 0)
        self.assertEqual(self.rows("SELECT id, approved_by_staff_id, approved_at FROM email_queue WHERE bulk_batch_id = %s ORDER BY id", (batch["batch_id"],)), before)

    def test_bulk_retry_returns_original_ids_after_employee_changes(self):
        identifier = uuid4()
        inputs = self.inputs()
        original = self.employees.register_bulk(self.admin_context, identifier, inputs)
        self.employees.update(self.admin_context, original["employees"][0]["employee_id"], full_name="Updated Name")
        replay = self.employees.register_bulk(self.admin_context, identifier, inputs)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["batch_id"], original["batch_id"])
        self.assertEqual(replay["employees"], original["employees"])

    def test_changed_bulk_payload_cannot_reuse_committed_request(self):
        from meal_management.errors import DomainError

        identifier = uuid4()
        inputs = self.inputs()
        batch = self.employees.register_bulk(self.admin_context, identifier, inputs)
        inputs[0]["full_name"] = "Changed Request"
        with self.assertRaisesRegex(DomainError, "IDEMPOTENCY_KEY_REUSED"):
            self.employees.register_bulk(self.admin_context, identifier, inputs)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue WHERE bulk_batch_id = %s", (batch["batch_id"],)), 2)

    def test_simultaneous_bulk_retries_create_one_committed_batch(self):
        email = "batch-admin-" + uuid4().hex + "@example.test"
        password = secrets.token_urlsafe(32)
        self.staff.create_staff(self.admin_context, "Batch Administrator", email, password, {"ADMIN"})
        contexts = [self.staff.authenticate(email, password) for _ in range(2)]
        identifier, inputs = uuid4(), self.inputs()
        barrier = threading.Barrier(2)
        def register(context):
            barrier.wait(timeout=10)
            return self.employees.register_bulk(context, identifier, inputs)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register, context) for context in contexts]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(results[0]["employees"], results[1]["employees"])
        self.assertEqual(results[0]["batch_id"], results[1]["batch_id"])

    def test_conflicting_late_employee_rolls_back_batch_employees_qrs_emails_and_audits(self):
        from meal_management.errors import DomainError

        registration, _ = self.employee()
        existing_code = self.scalar("SELECT employee_code FROM employees WHERE id = %s", (registration.employee_id,))
        tables = ("employee_email_batches", "employees", "qr_credentials", "email_queue", "audit_events")
        before = {table: self.scalar("SELECT COUNT(*) FROM " + table) for table in tables}
        inputs = self.inputs()
        inputs[1]["employee_code"] = existing_code
        with self.assertRaisesRegex(DomainError, "EMPLOYEE_CODE_EXISTS"):
            self.employees.register_bulk(self.admin_context, uuid4(), inputs)
        self.assertEqual({table: self.scalar("SELECT COUNT(*) FROM " + table) for table in tables}, before)

    def test_invalid_bulk_selection_does_not_partially_approve_other_rows(self):
        from meal_management.errors import DomainError

        batch = self.batch()
        first, second = batch["employees"]
        self.employees.set_active(self.admin_context, second["employee_id"], False)
        with self.assertRaisesRegex(DomainError, "EMAIL_APPROVAL_REQUIRED"):
            self.qr.email_queue.approve_bulk(self.admin_context, [first["email_id"], second["email_id"]])
        row = self.rows("SELECT status, approved_by_staff_id FROM email_queue WHERE id = %s", (first["email_id"],))[0]
        self.assertEqual(row, {"status": "PENDING_APPROVAL", "approved_by_staff_id": None})

    def test_waiter_cannot_register_or_approve_and_bulk_cannot_select_single(self):
        from meal_management.errors import DomainError

        registration, _ = self.employee()
        for action in (
            lambda: self.employees.register_bulk(self.waiter_context, uuid4(), self.inputs()),
            lambda: self.qr.email_queue.approve_single(self.waiter_context, registration.email_id, registration.employee_id),
            lambda: self.qr.email_queue.approve_bulk(self.waiter_context, [registration.email_id]),
            lambda: self.qr.email_queue.approve_bulk(self.admin_context, [registration.email_id]),
        ):
            with self.assertRaises(DomainError):
                action()

    def test_mysql_requires_approval_and_freezes_attribution(self):
        from mysql.connector import Error

        registration, _ = self.employee()
        with self.assertRaises(Error), self.db.transaction() as tx:
            tx.execute("UPDATE email_queue SET status = 'QUEUED' WHERE id = %s", (registration.email_id,))
        self.qr.email_queue.approve_single(self.admin_context, registration.email_id, registration.employee_id)
        for statement, parameters in (
            ("UPDATE email_queue SET approved_by_staff_id = %s WHERE id = %s", (self.waiter_id, registration.email_id)),
            ("UPDATE email_queue SET approved_at = NULL WHERE id = %s", (registration.email_id,)),
            ("UPDATE email_queue SET delivery_mode = 'LEGACY' WHERE id = %s", (registration.email_id,)),
        ):
            with self.assertRaises(Error), self.db.transaction() as tx:
                tx.execute(statement, parameters)

    def test_concurrent_single_resend_reuses_one_draft(self):
        registration, _ = self.employee()
        barrier = threading.Barrier(2)
        def resend():
            barrier.wait(timeout=10)
            return self.qr.resend_employee(self.admin_context, registration.employee_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resend) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results, [registration.email_id, registration.email_id])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue WHERE qr_id = %s", (registration.qr_id,)), 1)

    def test_uncertain_claim_status_is_safe_and_prevents_new_resend(self):
        from meal_management.errors import DomainError

        registration, _ = self.employee()
        self.qr.email_queue.approve_single(self.admin_context, registration.email_id, registration.employee_id)
        with self.db.transaction() as tx:
            tx.execute("UPDATE email_queue SET status = 'FAILED', last_error = %s WHERE id = %s", ("EMAIL_DELIVERY_CLAIMED_private-marker", registration.email_id))
        status = self.qr.email_queue.status(self.admin_context, registration.email_id, registration.employee_id)
        listed = self.query().email_status(self.admin_context, employee_id=registration.employee_id)["items"][0]
        self.assertEqual(status["status"], "NEEDS_REVIEW")
        self.assertEqual(listed["status"], "NEEDS_REVIEW")
        self.assertNotIn("private-marker", repr((status, listed)))
        with self.assertRaisesRegex(DomainError, "EMAIL_DELIVERY_NEEDS_REVIEW"):
            self.qr.resend_employee(self.admin_context, registration.employee_id)


class LegacyEmailApprovalMigrationMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        schema_path = root / "database" / "schema.sql"
        original_read = Path.read_text
        previous = "\n".join(path.read_text(encoding="utf-8") for path in sorted((root / "database" / "migrations").glob("*.sql")) if int(path.name[:3]) < 6)
        def schema_before_approval(path, *args, **kwargs):
            return previous if path == schema_path else original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", schema_before_approval):
            super().setUpClass()

    def test_migration_preserves_existing_email_payloads_and_holds_only_queued_rows(self):
        with self.db.transaction() as tx:
            employee_id = tx.insert(
                "INSERT INTO employees (employee_code, full_name, email, department_id) VALUES (%s, %s, %s, %s)",
                ("LEGACY-EMAIL", "Legacy Employee", "legacy@example.test", self.department_id),
            )
            credential = self.qr._issue(tx, self.admin_id, "EMPLOYEE", employee_id, None)
            encrypted = self.vault.encrypt(json.dumps({"employee_id": employee_id, "employee_name": "Legacy Employee", "token": credential.token, "qr_svg": "<svg/>"}))
            for status in ("QUEUED", "FAILED", "SENT", "CANCELLED"):
                tx.insert(
                    "INSERT INTO email_queue (qr_id, recipient_email, payload_ciphertext, status, last_error) VALUES (%s, %s, %s, %s, %s)",
                    (credential.qr_id, "legacy@example.test", encrypted, status, "Preserved historical marker"),
                )
        before = self.rows("SELECT id, qr_id, recipient_email, payload_ciphertext, status, created_at, last_error FROM email_queue ORDER BY id")
        migration = Path(__file__).resolve().parents[2] / "database" / "migrations" / "006_employee_email_approval.sql"
        cursor = self.admin_connection.cursor()
        try:
            for statement in schema_statements(migration.read_text(encoding="utf-8")):
                cursor.execute(statement)
        finally:
            cursor.close()
        after = self.rows("SELECT * FROM email_queue ORDER BY id")
        self.assertEqual(len(before), len(after))
        for original, updated in zip(before, after):
            for key in ("id", "qr_id", "recipient_email", "payload_ciphertext", "created_at", "last_error"):
                self.assertEqual(updated[key], original[key])
            self.assertEqual(updated["status"], "PENDING_APPROVAL" if original["status"] == "QUEUED" else original["status"])
            self.assertEqual(updated["delivery_mode"], "LEGACY")
            self.assertIsNone(updated["bulk_batch_id"])
            self.assertIsNone(updated["approved_by_staff_id"])
            self.assertIsNone(updated["approved_at"])
