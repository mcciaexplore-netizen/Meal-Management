import email
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path
from uuid import uuid4

from test_mysql_services import MealServicesFixture

MIGRATIONS = Path(__file__).resolve().parents[2] / "database" / "migrations"


class RecordingDelivery:
    enabled = True

    def __init__(self):
        self.messages = []

    def send(self, preview, *, allow_real_email=False):
        from meal_management.delivery import _qr_message

        recipient, identifier, wire = _qr_message("office@mcciapune.in", preview)
        self.messages.append((recipient, email.message_from_bytes(wire, policy=policy.default)))
        return identifier


class VisitorBulkMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.queries import QueryService

        cls.types = {row["code"]: row["id"] for row in cls.rows_static("SELECT id, code FROM meal_types")}
        cls.queries = QueryService(cls.db)

    @classmethod
    def rows_static(cls, statement, parameters=()):
        cursor = cls.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def service(self, limit=450):
        from meal_management.visitor_batches import VisitorBatchService

        return VisitorBatchService(self.db, self.qr, limit)

    def expiry(self, days=2):
        return datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(days=days)

    def visitor_rows(self, count=3, emails=True):
        tag = uuid4().hex[:6]
        return [{"row": index + 2, "sr_no": str(index + 1), "person_name": f"Guest {tag} {index}", "company_name": "Bharat Forge",
                 "phone": f"+91 98{secrets.randbelow(10**8):08d}", "email": f"guest{index}.{tag}@mcciapune.in" if emails and index != 1 else ""}
                for index in range(count)]

    def create(self, rows=None, request_id=None, service=None, **changes):
        values = {"expires_at": self.expiry(), "meal_type_ids": [self.types["LUNCH"], self.types["DINNER"]],
                  "department_id": self.department_id, "event_name": "Engineering Expo", "source_name": "guests.csv"}
        values.update(changes)
        return (service or self.service()).create(
            self.admin_context, request_id or uuid4(), rows if rows is not None else self.visitor_rows(), values["expires_at"],
            values["meal_type_ids"], values["department_id"], event_name=values["event_name"], source_name=values["source_name"])

    def batch_rows(self, batch_id):
        return self.rows(
            "SELECT a.qr_id, a.batch_row, a.email, a.department_id, a.event_name, a.meal_limit, c.expires_at, "
            "(SELECT COUNT(*) FROM master_qr_meal_types t WHERE t.qr_id = a.qr_id AND t.meal_limit = 1) AS types, "
            "(SELECT GROUP_CONCAT(q.status, ':', q.delivery_mode) FROM email_queue q WHERE q.qr_id = a.qr_id) AS emails "
            "FROM master_qr_allocations a JOIN qr_credentials c ON c.id = a.qr_id WHERE a.batch_id = %s ORDER BY a.batch_row",
            (batch_id,))

    def test_one_individual_qr_per_row_with_department_event_expiry_and_email_drafts(self):
        result = self.create()
        self.assertFalse(result["duplicate"])
        rows = self.batch_rows(result["batch_id"])
        self.assertEqual([row["batch_row"] for row in rows], [1, 2, 3])
        self.assertTrue(all(row["types"] == 2 and row["meal_limit"] == 1 and row["department_id"] == self.department_id
                            and row["event_name"] == "Engineering Expo" and row["expires_at"] is not None for row in rows))
        self.assertEqual([row["emails"] for row in rows], ["PENDING_APPROVAL:BULK", None, "PENDING_APPROVAL:BULK"])
        self.assertIsNone(rows[1]["email"])
        listed = self.service().list(self.admin_context)
        item = next(entry for entry in listed["items"] if entry["id"] == result["batch_id"])
        self.assertEqual((item["row_count"], item["emails_pending"], item["department_name"]), (3, 2, "Integration Department"))
        self.assertEqual(listed["email_allowance"]["daily_limit"], 450)
        detail = self.service().detail(self.admin_context, result["batch_id"])
        self.assertEqual([person["email_status"] for person in detail["people"]], ["PENDING_APPROVAL", None, "PENDING_APPROVAL"])
        self.assertEqual([person["qr_status"] for person in detail["people"]], ["ACTIVE"] * 3)

    def test_create_revalidates_everything_and_writes_nothing_on_error(self):
        from meal_management.errors import DomainError

        before = self.scalar("SELECT COUNT(*) FROM qr_credentials")
        bad = self.visitor_rows()
        bad[2]["email"] = "not-an-email"
        duplicate = self.visitor_rows()
        duplicate[2]["phone"] = duplicate[0]["phone"]
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute("INSERT INTO departments (name, is_active) VALUES (%s, FALSE)", ("Retired " + uuid4().hex[:6],))
            retired = cursor.lastrowid
        finally:
            cursor.close()
        for kwargs, code in (({"rows": bad}, "VISITOR_ROWS_INVALID"), ({"rows": duplicate}, "VISITOR_ROWS_INVALID"),
                             ({"department_id": retired}, "DEPARTMENT_UNAVAILABLE"),
                             ({"expires_at": self.expiry(days=-1)}, "INVALID_QR_EXPIRY"),
                             ({"meal_type_ids": []}, "INVALID_MEAL_TYPE_IDS"), ({"expires_at": None}, "VISITOR_BATCH_EXPIRY_REQUIRED")):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                self.create(**kwargs)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), before)

    def test_resubmitting_never_creates_duplicates(self):
        from meal_management.errors import DomainError

        rows, request_id, expiry = self.visitor_rows(), uuid4(), self.expiry()
        first = self.create(rows=rows, request_id=request_id, expires_at=expiry)
        count = self.scalar("SELECT COUNT(*) FROM qr_credentials")
        again = self.create(rows=rows, request_id=request_id, expires_at=expiry)
        self.assertEqual(again, {"batch_id": first["batch_id"], "duplicate": True})
        same_file = self.create(rows=rows, expires_at=expiry)
        self.assertEqual(same_file, {"batch_id": first["batch_id"], "duplicate": True})
        changed = [dict(row) for row in rows]
        changed[0]["person_name"] = "Someone Else"
        with self.assertRaisesRegex(DomainError, "REQUEST_MISMATCH"):
            self.create(rows=changed, request_id=request_id, expires_at=expiry)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), count)
        barrier, rows, request_id = threading.Barrier(3), self.visitor_rows(), uuid4()

        def submit(_):
            barrier.wait(timeout=10)
            return self.create(rows=rows, request_id=request_id, expires_at=expiry)["batch_id"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            identifiers = set(pool.map(submit, range(3)))
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM visitor_qr_batches WHERE id = %s", (identifiers.pop(),)), 1)

    def test_visitor_qr_without_email_scans_and_creates_no_email(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        credential = self.qr.issue_master(self.admin_context, company_name="Walk-in", contact_name="No Email",
                                          email="  ", phone="+91 98765 43210", meal_limit=1,
                                          meal_type_ids=[self.types["LUNCH"]], department_id=self.department_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue WHERE qr_id = %s", (credential.qr_id,)), 0)
        shared = ScanAppService(self.db, self.meals, ScanReceiptService(self.db))
        result = shared.read(secrets.token_bytes(32), uuid4(), credential.token, self.types["LUNCH"])
        self.assertTrue(result.approved, result.code)
        self.assertIsNone(self.scalar("SELECT visitor_email FROM servings WHERE id = %s", (result.serving_id,)))
        single = self.qr.issue_master(self.admin_context, company_name="Guest", contact_name="With Email",
                                      email="guest@mcciapune.in", phone="+91 98765 43211", meal_limit=1,
                                      meal_type_ids=[self.types["LUNCH"]], department_id=self.department_id, event_name="Expo")
        self.assertEqual(self.rows("SELECT status, delivery_mode FROM email_queue WHERE qr_id = %s", (single.qr_id,)),
                         [{"status": "DRAFT", "delivery_mode": "SINGLE"}])

    def worker(self, limit=450):
        from meal_management.email_worker import EmailDeliveryWorker

        delivery = RecordingDelivery()
        return EmailDeliveryWorker(self.db, self.vault, self.qr.renderer, delivery, daily_limit=limit), delivery

    def test_draft_then_send_builds_the_inline_html_email(self):
        from meal_management.errors import DomainError

        result = self.create()
        service = self.service()
        with self.assertRaisesRegex(DomainError, "EMAIL_COUNT_CHANGED"):
            service.approve_emails(self.admin_context, result["batch_id"], 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue q JOIN master_qr_allocations a ON a.qr_id = q.qr_id "
                                     "WHERE a.batch_id = %s AND q.status = 'QUEUED'", (result["batch_id"],)), 0)
        approved = service.approve_emails(self.admin_context, result["batch_id"], 2)
        self.assertEqual(approved["approved_count"], 2)
        worker, delivery = self.worker()
        outcomes = [worker.send(identifier, allow_real_email=True)["status"] for identifier in approved["email_ids"]]
        self.assertEqual(outcomes, ["SENT", "SENT"])
        recipient, message = delivery.messages[0]
        self.assertTrue(recipient.endswith("@mcciapune.in"))
        self.assertEqual(message["Subject"], "Your meal QR for Engineering Expo")
        plain, related = message.get_payload()
        html_part, image = related.get_payload()
        self.assertIn('src="cid:' + image["Content-ID"].strip("<>") + '"', html_part.get_content())
        for text in ("Integration Department", "Engineering Expo", "IST (UTC+05:30)", "(one each)"):
            self.assertIn(text, plain.get_content())
        detail = self.service().detail(self.admin_context, result["batch_id"])
        self.assertEqual([person["email_status"] for person in detail["people"]], ["SENT", None, "SENT"])
        with self.assertRaisesRegex(DomainError, "VISITOR_BATCH_NOTHING_TO_SEND"):
            service.approve_emails(self.admin_context, result["batch_id"], 2)

    def test_daily_limit_counts_every_sent_email_and_blocks_oversized_batches(self):
        from meal_management.errors import DomainError

        sent_today = self.scalar("SELECT COUNT(*) FROM email_queue WHERE status = 'SENT' AND updated_at >= UTC_TIMESTAMP(6) - INTERVAL 1 DAY")
        queued = self.scalar("SELECT COUNT(*) FROM email_queue WHERE status = 'QUEUED'")
        result = self.create(rows=self.visitor_rows(4))
        with self.assertRaisesRegex(DomainError, "EMAIL_DAILY_LIMIT_EXCEEDED"):
            self.service(limit=sent_today + queued + 2).approve_emails(self.admin_context, result["batch_id"], 3)
        approved = self.service(limit=sent_today + queued + 3).approve_emails(self.admin_context, result["batch_id"], 3)
        worker, delivery = self.worker(limit=sent_today + 1)
        outcomes = [worker.send(identifier, allow_real_email=True) for identifier in approved["email_ids"]]
        self.assertEqual([item["status"] for item in outcomes], ["SENT", "QUEUED", "QUEUED"])
        self.assertEqual(outcomes[1]["code"], "EMAIL_DAILY_LIMIT_REACHED")
        self.assertEqual(len(delivery.messages), 1)

    def test_single_email_and_resend_cap(self):
        from meal_management.errors import DomainError

        single = self.qr.issue_master(self.admin_context, company_name="Guest", contact_name="Resend Guest",
                                      email="resend@mcciapune.in", phone="+91 98765 43212", meal_limit=1,
                                      meal_type_ids=[self.types["LUNCH"]], department_id=self.department_id)
        worker, delivery = self.worker()
        service = self.service()
        for attempt in range(4):
            email_id = service.prepare_single(self.admin_context, single.qr_id)
            self.assertEqual(worker.send(email_id, allow_real_email=True)["status"], "SENT", attempt)
        with self.assertRaisesRegex(DomainError, "EMAIL_RESEND_LIMIT_REACHED"):
            service.prepare_single(self.admin_context, single.qr_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue WHERE qr_id = %s", (single.qr_id,)), 4)
        self.assertEqual(len(delivery.messages), 4)
        no_email = self.qr.issue_master(self.admin_context, company_name="Guest", contact_name="No Email", email=None,
                                        phone="+91 98765 43213", meal_limit=1, meal_type_ids=[self.types["LUNCH"]],
                                        department_id=self.department_id)
        with self.assertRaisesRegex(DomainError, "VISITOR_EMAIL_MISSING"):
            service.prepare_single(self.admin_context, no_email.qr_id)

    def test_email_off_keeps_drafts_and_sends_nothing(self):
        from meal_management.email_actions import EmailActions
        from meal_management.errors import DomainError
        from meal_management.runtime import RuntimeSettings
        from types import SimpleNamespace

        runtime = SimpleNamespace(email_send_enabled=False, email_backend="gmail", email_daily_limit=450)
        actions = EmailActions(runtime, SimpleNamespace(database=self.db, qr=self.qr))
        result = self.create()
        single = self.qr.issue_master(self.admin_context, company_name="Guest", contact_name="Off Guest",
                                      email="off@mcciapune.in", phone="+91 98765 43214", meal_limit=1,
                                      meal_type_ids=[self.types["LUNCH"]], department_id=self.department_id)
        for call in (lambda: actions.send_visitor(self.admin_context, single.qr_id, self.service()),
                     lambda: actions.approve_visitor_batch(self.admin_context, self.service(), result["batch_id"], 2)):
            with self.assertRaisesRegex(DomainError, "REAL_EMAIL_NOT_AUTHORIZED"):
                call()
        self.assertEqual(self.scalar("SELECT status FROM email_queue WHERE qr_id = %s", (single.qr_id,)), "DRAFT")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue q JOIN master_qr_allocations a ON a.qr_id = q.qr_id "
                                     "WHERE a.batch_id = %s AND q.status = 'PENDING_APPROVAL'", (result["batch_id"],)), 2)

    def test_worker_refuses_a_revoked_visitor_qr(self):
        single = self.qr.issue_master(self.admin_context, company_name="Guest", contact_name="Revoked",
                                      email="revoked@mcciapune.in", phone="+91 98765 43215", meal_limit=1,
                                      meal_type_ids=[self.types["LUNCH"]], department_id=self.department_id)
        email_id = self.service().prepare_single(self.admin_context, single.qr_id)
        self.qr.revoke(self.admin_context, single.qr_id, "Test")
        worker, delivery = self.worker()
        self.assertIn(worker.send(email_id, allow_real_email=True)["status"], {"CANCELLED", "FAILED"})
        self.assertEqual(delivery.messages, [])

    def test_meal_records_show_and_filter_department_for_employees_and_visitors(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        other = self.employees.create_department(self.admin_context, "Visitor Dept " + uuid4().hex[:6])
        visitor = self.qr.issue_master(self.admin_context, company_name="Guest Co", contact_name="Dept Guest", email=None,
                                       phone="+91 98765 43216", meal_limit=1, meal_type_ids=[self.types["LUNCH"]],
                                       department_id=other, event_name="Expo")
        shared = ScanAppService(self.db, self.meals, ScanReceiptService(self.db))
        visitor_meal = shared.read(secrets.token_bytes(32), uuid4(), visitor.token, self.types["LUNCH"]).meal_ids[0]
        _, token = self.employee()
        employee_meal = self.meals.record(self.waiter_context, self.scan(token)).meal_ids[0]
        now = datetime.now(timezone.utc)
        items = {item["meal_id"]: item for item in self.queries.meal_history(self.admin_context, now - timedelta(days=1), now + timedelta(days=1), limit=1000)["items"]}
        self.assertEqual((items[visitor_meal]["department_id"], items[visitor_meal]["event_name"]), (other, "Expo"))
        self.assertEqual(items[employee_meal]["department_name"], "Integration Department")
        filtered = self.queries.meal_history(self.admin_context, now - timedelta(days=1), now + timedelta(days=1), limit=1000, department_id=other)["items"]
        self.assertEqual([item["meal_id"] for item in filtered], [visitor_meal])
        detail = self.queries.qr_metadata(self.admin_context, visitor.qr_id)
        self.assertEqual((detail["department_name"], detail["event_name"], detail["email_status"]), (filtered[0]["department_name"], "Expo", None))


class VisitorMigrationMySQLTests(MealServicesFixture):
    def test_migration_015_keeps_existing_visitor_qrs_with_no_department_and_relaxes_email(self):
        from meal_management.accounts import StaffService
        from meal_management.database import Database
        from meal_management.migrations import MigrationRunner
        from meal_management.qr import QrService
        from meal_management.security import QrRenderer

        name = f"{self.database_name[:40]}_{uuid.uuid4().hex[:6]}_test"
        cursor = self.admin_connection.cursor()
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        cursor.close()
        self.addCleanup(self._drop, name)
        database = Database(replace(self.admin_settings, db_name=name))
        MigrationRunner(database, MIGRATIONS, through=14).apply()
        staff = StaffService(database)
        password = secrets.token_urlsafe(24)
        admin = staff.bootstrap_admin("Migration Admin", "migration@example.test", password)
        context = staff.authenticate("migration@example.test", password)
        qr = QrService(database, self.vault, QrRenderer())
        with database.transaction() as tx:
            qr_id = qr._issue(tx, admin, "MASTER", None, None).qr_id
            tx.execute("INSERT INTO master_qr_allocations (qr_id, company_name, contact_name, email, phone, meal_limit, "
                       "created_by_staff_id) VALUES (%s, 'Example', 'Lead', 'lead@example.test', '+919876543210', 3, %s)",
                       (qr_id, admin))
        self.assertEqual(MigrationRunner(database, MIGRATIONS).apply(), (15, 16))
        with database.transaction() as tx:
            row = tx.one("SELECT email, department_id, event_name, batch_id, batch_row, meal_limit FROM master_qr_allocations WHERE qr_id = %s", (qr_id,))
            self.assertEqual(row, {"email": "lead@example.test", "department_id": None, "event_name": None, "batch_id": None,
                                   "batch_row": None, "meal_limit": 3})
            tx.execute("UPDATE master_qr_allocations SET email = NULL WHERE qr_id = %s", (qr_id,))
        self.assertIsNotNone(context)

    def _drop(self, name):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE `{name}`")
        finally:
            cursor.close()
