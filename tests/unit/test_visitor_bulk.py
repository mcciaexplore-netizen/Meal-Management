import email
import io
import unittest
import zipfile
from datetime import datetime, timezone
from email import policy
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from meal_management.delivery import _qr_message
from meal_management.email_queue import EmailPreview
from meal_management.email_worker import EmailDeliveryWorker
from meal_management.errors import DomainError
from meal_management.runtime import RuntimeSettings
from meal_management.security import generate_token
from meal_management.visitor_batches import MAX_ROWS, read_rows, validate_rows
from meal_management import visitor_email
from test_api import ApiHarness, ApiTestCase
from test_runtime_storage import environment

HEADER = "Sr. No.,Person Name,Company Name,Phone,Email\n"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def csv_bytes(*rows, bom=False):
    text = HEADER + "".join(",".join(row) + "\n" for row in rows)
    return ("﻿" if bom else "").encode() + text.encode()


def xlsx_bytes(rows, formula=False):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Sr. No.", "Person Name", "Company Name", "Phone", "Email"])
    for row in rows:
        sheet.append(row)
    if formula:
        sheet["A3"] = "=1+1"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def with_entry(data, name, content=b"<x/>"):
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(name, content)
    return output.getvalue()


class VisitorFileTests(unittest.TestCase):
    def test_csv_with_bom_and_blank_trailing_rows_is_read(self):
        rows = read_rows(csv_bytes(("1", "Asha Rao", "Bharat Forge", "+91 98765 43210", "asha@example.org"),
                                   ("", "", "", "", ""), bom=True), "text/csv", "guests.csv")
        self.assertEqual(rows, [{"row": 2, "sr_no": "1", "person_name": "Asha Rao", "company_name": "Bharat Forge",
                                 "phone": "+91 98765 43210", "email": "asha@example.org"}])

    def test_headers_size_count_and_type_are_enforced(self):
        cases = (
            (b"Name,Company\nA,B\n", "text/csv", "a.csv", "VISITOR_FILE_HEADERS"),
            (csv_bytes(("1", "A", "B", "9876543210", "", "extra")), "text/csv", "a.csv", "VISITOR_FILE_HEADERS"),
            (HEADER.encode(), "text/csv", "a.csv", "VISITOR_FILE_EMPTY"),
            (csv_bytes(*[(str(i), "A", "B", "98765432%02d" % (i % 100), "") for i in range(1, MAX_ROWS + 2)]),
             "text/csv", "a.csv", "VISITOR_FILE_TOO_MANY_ROWS"),
            (b"x" * 1_048_577, "text/csv", "a.csv", "VISITOR_FILE_TOO_LARGE"),
            (b"", "text/csv", "a.csv", "VISITOR_FILE_REQUIRED"),
            (csv_bytes(("1", "A", "B", "9876543210", "")), "text/csv", "a.xls", "VISITOR_FILE_TYPE"),
            (csv_bytes(("1", "A", "B", "9876543210", "")), "text/csv", "a.xlsm", "VISITOR_FILE_TYPE"),
            (b"\xff\xfe\x00bad", "text/csv", "a.csv", "VISITOR_FILE_ENCODING"),
        )
        for data, content_type, name, code in cases:
            with self.subTest(code=code, name=name), self.assertRaisesRegex(DomainError, code):
                read_rows(data, content_type, name)

    def test_xlsx_values_are_read_and_unsafe_workbooks_are_refused(self):
        data = xlsx_bytes([[1, "Asha Rao", "Bharat Forge", "+91 98765 43210", "asha@example.org"]])
        rows = read_rows(data, XLSX, "guests.xlsx")
        self.assertEqual(rows[0]["sr_no"], "1")
        self.assertEqual(rows[0]["person_name"], "Asha Rao")
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", b"<w/>")
            archive.writestr("xl/huge.xml", b"0" * (21 * 1024 * 1024))
        for payload, code in (
            (xlsx_bytes([[1, "A", "B", "9876543210", ""], [2, "C", "D", "9876543211", ""]], formula=True), "VISITOR_FILE_FORMULAS"),
            (with_entry(data, "xl/externalLinks/externalLink1.xml"), "VISITOR_FILE_EXTERNAL_LINKS"),
            (with_entry(data, "xl/vbaProject.bin", b"macro"), "VISITOR_FILE_MACROS"),
            (bomb.getvalue(), "VISITOR_FILE_INVALID"),
            (b"PK\x03\x04not really a zip", "VISITOR_FILE_INVALID"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                read_rows(payload, XLSX, "guests.xlsx")
        with self.assertRaisesRegex(DomainError, "VISITOR_FILE_TYPE"):
            read_rows(csv_bytes(("1", "A", "B", "9876543210", "")), XLSX, "guests.xlsx")


class VisitorRowValidationTests(unittest.TestCase):
    def row(self, number, sr, name="Asha", company="Bharat Forge", phone="+91 98765 43210", mail=""):
        return {"row": number, "sr_no": sr, "person_name": name, "company_name": company, "phone": phone, "email": mail}

    def test_every_row_problem_is_reported_with_its_row_and_column(self):
        rows = [
            self.row(2, "1", mail="ASHA@Example.org"),
            self.row(3, "x", name="", company="C" * 151, phone="12", mail="not-an-email"),
            self.row(4, "1", phone="+919876543210", mail="asha@example.org"),
            self.row(5, "4", phone="98765 43219"),
        ]
        clean, errors = validate_rows(rows)
        found = {(item["row"], item["column"], item["code"]) for item in errors}
        self.assertEqual(found, {
            (3, "Sr. No.", "INVALID_SR_NO"), (3, "Person Name", "REQUIRED"), (3, "Company Name", "TOO_LONG"),
            (3, "Phone", "INVALID_PHONE"), (3, "Email", "INVALID_EMAIL"),
            (2, "Sr. No.", "DUPLICATE_SR_NO"), (4, "Sr. No.", "DUPLICATE_SR_NO"),
            (2, "Phone", "DUPLICATE_PHONE"), (4, "Phone", "DUPLICATE_PHONE"),
            (2, "Email", "DUPLICATE_EMAIL"), (4, "Email", "DUPLICATE_EMAIL"),
        })
        self.assertTrue(all(item["message"].startswith(item["column"]) for item in errors))
        self.assertEqual(clean[3]["email"], None)
        self.assertEqual(clean[0]["email"], "asha@example.org")

    def test_valid_rows_are_normalized(self):
        clean, errors = validate_rows([self.row(2, " 7 ", name=" Asha Rao ", mail=" Asha@Example.ORG ")])
        self.assertEqual(errors, [])
        self.assertEqual(clean[0], {"row": 2, "sr_no": 7, "person_name": "Asha Rao", "company_name": "Bharat Forge",
                                    "phone": "+91 98765 43210", "email": "asha@example.org"})
        for rows in ([], [self.row(2, str(i)) for i in range(1, MAX_ROWS + 2)], ["not a row"], [{"row": 2, "sr_no": True}]):
            with self.subTest(count=len(rows)), self.assertRaises(DomainError):
                validate_rows(rows)


class VisitorEmailTemplateTests(unittest.TestCase):
    visitor = {"company_name": 'Acme <script>alert(1)</script>', "department_name": "Exhibition Centre",
               "event_name": 'Expo "2026" & <b>Friends</b>', "meal_types": ["Breakfast", "High Tea"],
               "expires_at": "2026-10-31T12:30:00+00:00"}

    def test_fields_are_escaped_and_times_shown_in_india_time(self):
        html = visitor_email.html_body('Priya <img src=x onerror=alert(1)>', self.visitor, "cid:abc@x")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("Expo &quot;2026&quot; &amp; &lt;b&gt;Friends&lt;/b&gt;", html)
        self.assertIn("31 Oct 2026, 6:00 PM IST (UTC+05:30)", html)
        self.assertIn('src="cid:abc@x"', html)
        self.assertNotIn("http", html.lower().replace("http-equiv", ""))
        self.assertNotIn("data:", html)
        text = visitor_email.text_body("Priya", self.visitor)
        self.assertIn("Meals: Breakfast, High Tea (one each)", text)
        self.assertIn("Event: Expo \"2026\" & <b>Friends</b>", text)

    def test_subject_and_optional_lines(self):
        self.assertEqual(visitor_email.subject(self.visitor), 'Your meal QR for Expo "2026" & <b>Friends</b>')
        plain = {**self.visitor, "event_name": None, "department_name": None, "expires_at": None}
        self.assertEqual(visitor_email.subject(plain), "Your visitor meal QR")
        text = visitor_email.text_body("Priya", plain)
        self.assertNotIn("Event:", text)
        self.assertNotIn("Department:", text)
        self.assertIn("Valid until: Until every approved meal is used", text)
        for unsafe in ({**self.visitor, "event_name": "Expo\r\nBcc: x@y.z"}, ):
            with self.assertRaises(ValueError):
                visitor_email.subject(unsafe)
        with self.assertRaises(ValueError):
            visitor_email.text_body("Priya\nBcc: x", self.visitor)

    def test_message_is_html_with_text_alternative_and_inline_cid_qr(self):
        preview = EmailPreview(1, "guest@mcciapune.in", "Priya Sharma", generate_token(), "", kind="VISITOR",
                               visitor=self.visitor)
        _, identifier, wire = _qr_message("office@mcciapune.in", preview)
        message = email.message_from_bytes(wire, policy=policy.default)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(message["Subject"], 'Your meal QR for Expo "2026" & <b>Friends</b>')
        plain, related = message.get_payload()
        self.assertEqual((plain.get_content_type(), related.get_content_type()), ("text/plain", "multipart/related"))
        html_part, image = related.get_payload()
        self.assertEqual((html_part.get_content_type(), image.get_content_type()), ("text/html", "image/png"))
        content_id = image["Content-ID"].strip("<>")
        self.assertIn('src="cid:' + content_id + '"', html_part.get_content())
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertTrue(image.get_content().startswith(b"\x89PNG"))
        self.assertNotIn("data:", html_part.get_content())
        self.assertTrue(identifier.endswith("@mcciapune.in>"))


class DailyLimitTests(unittest.TestCase):
    def test_setting_defaults_follow_the_provider_and_are_bounded(self):
        self.assertEqual(RuntimeSettings.from_env(environment(EMAIL_BACKEND="preview")).email_daily_limit, 450)
        self.assertEqual(RuntimeSettings.from_env(environment(EMAIL_DAILY_LIMIT="80")).email_daily_limit, 80)
        for value in ("0", "10001", "many"):
            with self.subTest(value=value), self.assertRaises(Exception):
                RuntimeSettings.from_env(environment(EMAIL_DAILY_LIMIT=value))

    def test_worker_leaves_email_queued_once_the_day_is_used_up(self):
        database = MagicMock()
        delivery = Mock(enabled=True)
        worker = EmailDeliveryWorker(database, Mock(), Mock(), delivery, daily_limit=2)
        with patch.object(worker.queue, "sent_since", return_value=2) as sent, patch.object(worker, "_claim") as claim:
            result = worker.send(9, allow_real_email=True)
        self.assertEqual(result, {"email_id": 9, "status": "QUEUED", "code": "EMAIL_DAILY_LIMIT_REACHED"})
        sent.assert_called_once()
        claim.assert_not_called()
        delivery.send.assert_not_called()
        with self.assertRaises(DomainError):
            EmailDeliveryWorker(database, Mock(), Mock(), delivery, daily_limit=0)

    def test_dispatcher_stops_its_batch_at_the_limit(self):
        from meal_management.email_dispatcher import EmailDispatcher

        worker = Mock()
        worker.send.side_effect = lambda identifier, allow_real_email: {"email_id": identifier, "status": "QUEUED", "code": "EMAIL_DAILY_LIMIT_REACHED"}
        dispatcher = EmailDispatcher(MagicMock(), worker)
        with patch.object(dispatcher, "_enabled", return_value=True), patch.object(dispatcher, "_select", return_value=[4, 5]):
            result = dispatcher.dispatch_once()
        self.assertEqual((result["status"], result["deferred"], worker.send.call_count), ("LIMITED", 1, 1))


class VisitorBatchApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.batches = Mock()
        self.harness.app.state.visitor_batches = self.batches
        self.batches.preview.return_value = {"rows": [], "errors": [], "row_count": 1, "valid": True, "email_count": 0}
        self.batches.create.return_value = {"batch_id": 3, "duplicate": False}

    def body(self, **changes):
        values = {"request_id": str(uuid4()), "source_name": "guests.csv", "expires_at": "2026-10-31T18:00:00+05:30",
                  "meal_type_ids": [2, 3], "department_id": 4, "event_name": "Expo",
                  "rows": [{"row": 2, "sr_no": "1", "person_name": "Asha", "company_name": "B", "phone": "9876543210", "email": ""}]}
        values.update(changes)
        return values

    def test_preview_passes_the_raw_file_and_create_passes_every_setting(self):
        self.harness.login()
        response = self.client.post("/api/visitor-batches/preview?filename=guests.csv", content=csv_bytes(("1", "A", "B", "9876543210", "")),
                                    headers={**self.harness.headers(), "Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 200, response.text)
        args = self.batches.preview.call_args.args
        self.assertEqual((args[2], args[3]), ("text/csv", "guests.csv"))
        self.assertTrue(args[1].startswith(b"Sr. No."))
        response = self.harness.post("/api/visitor-batches", json=self.body())
        self.assertEqual(response.status_code, 201, response.text)
        args, kwargs = self.batches.create.call_args
        self.assertEqual(args[3].utcoffset().total_seconds(), 19800)
        self.assertEqual((args[4], args[5], kwargs["event_name"], kwargs["source_name"]), ([2, 3], 4, "Expo", "guests.csv"))

    def test_create_requires_every_setting_and_bounds_rows(self):
        self.harness.login()
        for body in (self.body(expires_at=None), self.body(department_id=None), self.body(meal_type_ids=[]),
                     self.body(rows=[]), self.body(rows=[self.body()["rows"][0]] * 201), self.body(request_id="x"),
                     self.body(event_name="e" * 151), {k: v for k, v in self.body().items() if k != "source_name"}):
            with self.subTest(keys=sorted(body)):
                self.assert_error(self.harness.post("/api/visitor-batches", json=body), 422, "INVALID_INPUT")
        self.batches.create.assert_not_called()

    def test_sending_needs_confirmation_and_admin_role(self):
        self.harness.login()
        self.assert_error(self.harness.post("/api/visitor-batches/3/send-emails", json={}), 422, "INVALID_INPUT")
        self.assert_error(self.harness.post("/api/master-qrs/5/email", json={"confirm": False}), 422, "INVALID_INPUT")
        calls = (
            lambda: self.client.get("/api/visitor-batches"),
            lambda: self.client.get("/api/visitor-batches/3"),
            lambda: self.harness.post("/api/visitor-batches", json=self.body()),
            lambda: self.harness.post("/api/visitor-batches/3/send-emails", json={"expected_count": 1}),
            lambda: self.harness.post("/api/master-qrs/5/email", json={"confirm": True}),
        )
        self.harness.login("waiter")
        for call in calls:
            self.assert_error(call(), 403, "ROLE_REQUIRED")

    def test_email_off_refuses_sending_and_keeps_drafts(self):
        self.harness.login()
        self.assert_error(self.harness.post("/api/master-qrs/5/email", json={"confirm": True}), 400, "REAL_EMAIL_NOT_AUTHORIZED")
        self.assert_error(self.harness.post("/api/visitor-batches/3/send-emails", json={"expected_count": 2}), 400, "REAL_EMAIL_NOT_AUTHORIZED")
        self.batches.prepare_single.assert_not_called()
        self.batches.approve_emails.assert_not_called()
        self.assertEqual(self.client.get("/api/email-settings").json()["sending_enabled"], False)

    def test_preview_body_limit_is_one_megabyte(self):
        self.harness.login()
        response = self.client.post("/api/visitor-batches/preview?filename=a.csv", content=b"x" * 1_048_577,
                                    headers={**self.harness.headers(), "Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 413)
        self.batches.preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
