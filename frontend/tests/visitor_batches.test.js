import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { COLUMNS, MAX_FILE_BYTES, batchSummary, checkFile, errorRows, requiredExpiry, uploadType } from "../src/visitor_batches.js";
import { SENDING_DISABLED, confirmSendText, departmentEventLine, emailActionLabel, emailStatusBadge } from "../src/visitor_qrs.js";

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");

test("only small csv and xlsx files are accepted", () => {
  assert.equal(uploadType({ name: "Guests.CSV" }), "text/csv");
  assert.equal(uploadType({ name: "guests.xlsx" }), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
  for (const name of ["guests.xls", "guests.xlsm", "guests.txt", ""]) assert.throws(() => uploadType({ name }), /\.csv or \.xlsx/);
  assert.throws(() => checkFile(null), /Choose/);
  assert.throws(() => checkFile({ name: "a.csv", size: MAX_FILE_BYTES + 1 }), /1 MB/);
  assert.throws(() => checkFile({ name: "a.csv", size: 0 }), /empty/);
  assert.deepEqual(COLUMNS, ["Sr. No.", "Person Name", "Company Name", "Phone", "Email"]);
});

test("bulk expiry is required and row errors are escaped", () => {
  assert.throws(() => requiredExpiry(""), /expiry/);
  assert.match(requiredExpiry("2026-10-31T18:00"), /^2026-10-31T\d\d:\d\d:00\.000Z$/);
  const html = errorRows([{ row: 3, column: "Email", message: "Email <b>bad</b>" }]).join("");
  assert.match(html, /Email &lt;b&gt;bad&lt;\/b&gt;/);
  assert.equal(batchSummary({ emails_pending: 2, emails_sent: 1, emails_failed: 0 }), "2 to send · 1 sent");
  assert.equal(batchSummary({}), "No emails");
});

test("email states, actions and the send confirmation", () => {
  assert.match(emailStatusBadge({ email: null }), /No email/);
  assert.match(emailStatusBadge({ email: "a@b.in", email_status: "FAILED", email_code: "EMAIL_RECIPIENT_REJECTED" }), /Failed.*email recipient rejected/s);
  assert.equal(emailActionLabel({ email: null }), null);
  assert.equal(emailActionLabel({ email: "a@b.in", email_status: "DRAFT" }), "Email QR");
  assert.equal(emailActionLabel({ email: "a@b.in", email_status: "QUEUED" }), null);
  assert.equal(emailActionLabel({ email: "a@b.in", email_status: "SENT", resends_left: 2 }), "Resend QR (2 left)");
  assert.equal(emailActionLabel({ email: "a@b.in", email_status: "SENT", resends_left: 0 }), null);
  assert.equal(confirmSendText(3, { remaining: 40, daily_limit: 450 }), "Send 3 emails? 40 of today's 450 emails remain.");
  assert.equal(confirmSendText(1), "Send 1 email?");
  assert.equal(departmentEventLine({ department_name: "Exhibition Centre", event_name: "Expo" }), "Exhibition Centre · Expo");
  assert.match(SENDING_DISABLED, /nothing has been sent/);
});

test("the visitor QR form asks for a department, an optional event and an optional email", async () => {
  const screens = await source("screens.js");
  assert.match(screens, /selectField\("Department", "department_id", employeeDepartmentOptions\(context\.catalog\?\.departments/);
  assert.match(screens, /field\("Event name \(optional\)", "event_name"/);
  assert.match(screens, /field\("Email \(optional\)", "email", values\?\.email \?\? "", "email", 'maxlength="254"/);
  assert.match(screens, /body\.department_id = await resolveEmployeeDepartment\(/);
  assert.match(screens, /<span>Department<\/span><strong>\$\{escape\(qr\.department_name/);
  assert.match(screens, /<span>Email status<\/span><strong>\$\{emailStatusBadge\(qr\)\}/);
  assert.match(screens, /api\(`\/master-qrs\/\$\{id\}\/email`, \{ method: "POST", body: \{ confirm: true \} \}\)/);
  assert.doesNotMatch(screens, /window\.confirm/);
  assert.match(screens, /"Category", "Department", "Service"/);
  assert.match(screens, /<label>Department<select name="department_id"><option value="">All departments<\/option>/);
});

test("the bulk page previews first, confirms the count and sends only when email is on", async () => {
  const page = await source("visitor_batches.js");
  assert.match(page, /\/visitor-batches\/preview\?/);
  assert.match(page, /if \(!result\.valid\)[\s\S]*Nothing was created/);
  assert.match(page, /request_id: requestId\(file\)/);
  assert.match(page, /body: \{ expected_count: pending \}/);
  assert.match(page, /\/email-queue\/process/);
  assert.match(page, /\$\{sending \? "" : "disabled"\}>Send emails/);
  assert.match(page, /SENDING_DISABLED/);
  const app = await source("app.js");
  assert.match(app, /\["visitor-batches", \["Bulk visitor QRs", "qr", visitorBatches\]\]/);
});
