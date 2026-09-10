import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { canApproveEmail, canPreviewEmail, canProcessEmail, canSendEmail, emailDeliverySettings, emailModeDescription, emailModeLabel, emailStatusDetail } from "../src/email_delivery.js";
import { emailTable } from "../src/screens.js";

const preview = { backend: "preview", sending_enabled: false, preview_available: true, approval_required: true };
const gmail = { backend: "gmail", sending_enabled: false, preview_available: false, approval_required: true };
const rows = ["DRAFT", "PENDING_APPROVAL", "QUEUED", "CANCELLED", "SENT", "FAILED"].map((status, index) => ({ id: index + 21, delivery_mode: status === "DRAFT" ? "SINGLE" : "BULK", recipient_email: "employee@example.test", status, created_at: "2026-09-09T10:00:00Z", approved_at: status === "QUEUED" ? "2026-09-10T01:00:00Z" : null }));

test("preview mode explains disabled real delivery and does not imply approval", () => {
  assert.match(emailModeLabel(preview), /Local previews only; no emails sent/);
  assert.match(emailModeDescription(preview), /does not deliver messages/);
  assert.match(emailModeDescription(preview), /does not approve or send/);
  assert.equal(canSendEmail(preview), false);
});

test("Gmail and SES explain explicit single sending and approved bulk processing", () => {
  for (const backend of ["gmail", "ses"]) {
    for (const enabled of [false, true]) {
      const settings = { ...gmail, backend, sending_enabled: enabled };
      assert.match(emailModeLabel(settings), enabled ? /sending enabled/ : /sending disabled/);
      assert.match(emailModeDescription(settings), enabled ? /only when you choose Send email/ : /delivery is disabled/);
      assert.match(emailModeDescription(settings), /bulk/i);
      assert.equal(canSendEmail(settings), enabled);
    }
  }
});

test("background delivery is restricted to approved bulk messages in explanatory copy", () => {
  const settings = emailDeliverySettings({ ...gmail, sending_enabled: true, automatic_enabled: true, worker_running: true, poll_seconds: 7, batch_size: 4 });
  assert.match(emailModeDescription(settings), /Individual drafts send only when you choose Send email/);
  assert.match(emailModeDescription(settings), /approved bulk messages about every 7 seconds/);
  assert.doesNotMatch(emailModeDescription(settings), /Registration and QR resends queue/);
  assert.match(emailModeDescription({ ...settings, worker_running: false }), /background sender is not running/);
});

test("invalid settings cannot bypass approval or imply running delivery", () => {
  for (const value of [null, {}, { ...gmail, approval_required: false }, { ...gmail, approval_required: undefined }, { ...gmail, backend: "unknown" }, { ...gmail, preview_available: true }, { ...gmail, sending_enabled: "true" }, { ...gmail, automatic_enabled: true }, { ...gmail, worker_running: true }, { ...preview, sending_enabled: true, automatic_enabled: true }, { ...gmail, poll_seconds: 0 }, { ...gmail, batch_size: 101 }]) assert.throws(() => emailDeliverySettings(value), /could not be confirmed/);
  assert.deepEqual(emailDeliverySettings({ ...gmail, sender: "private@example.test" }), { ...gmail, automatic_enabled: false, worker_running: false, poll_seconds: 5, batch_size: 10, process_batch_size: 10 });
});

test("manual processing uses its independently validated server limit", () => {
  assert.equal(emailDeliverySettings({ ...gmail, process_batch_size: 1, batch_size: 100 }).process_batch_size, 1);
  for (const process_batch_size of [null, true, false, 0, 11, 1.5, "1"]) assert.throws(() => emailDeliverySettings({ ...gmail, process_batch_size }), /processing settings could not be confirmed/);
});

test("only unsent messages in local preview mode expose Preview", () => {
  rows.forEach((row, index) => assert.equal(canPreviewEmail(row, preview), index < 3));
  for (const settings of [gmail, { ...preview, preview_available: false }, null]) for (const row of rows) assert.equal(canPreviewEmail(row, settings), false);
});

test("individual draft send actions are scoped to employee history and gated by real delivery", () => {
  const disabled = emailTable(rows, true, gmail, { single: true });
  assert.match(disabled, /data-action="email-send" data-id="21" disabled/);
  const enabled = emailTable(rows, true, { ...gmail, sending_enabled: true }, { single: true });
  assert.match(enabled, /data-action="email-send" data-id="21" >Send email/);
  assert.equal((enabled.match(/data-action="email-send"/g) ?? []).length, 1);
  assert.doesNotMatch(emailTable(rows, false, { ...gmail, sending_enabled: true }), /email-send/);
  const unknown = emailTable(rows, true, gmail, { single: true, unknown: new Set([21]) });
  assert.doesNotMatch(unknown, /email-send/);
  assert.match(unknown, /email-check/);
  assert.match(unknown, /RESULT UNCONFIRMED/);
});

test("bulk eligibility excludes individual drafts and unapproved queued rows", () => {
  assert.equal(canApproveEmail(rows[1]), true);
  assert.equal(canProcessEmail(rows[2]), true);
  for (const row of [{ ...rows[1], delivery_mode: "SINGLE" }, { ...rows[1], delivery_mode: undefined }, rows[0]]) assert.equal(canApproveEmail(row), false);
  for (const row of [{ ...rows[2], approved_at: null }, { ...rows[2], delivery_mode: "SINGLE" }, rows[1]]) assert.equal(canProcessEmail(row), false);
  const html = emailTable(rows, false, gmail, { select: true });
  assert.equal((html.match(/data-email-select=/g) ?? []).length, 2);
  assert.match(html, /Email ID/);
});

test("statuses distinguish drafts, approval, provider acceptance and uncertain delivery", () => {
  assert.match(emailStatusDetail("SENT"), /inbox delivery is not confirmed/);
  assert.match(emailStatusDetail("DRAFT"), /Not sent/);
  assert.match(emailStatusDetail("PENDING_APPROVAL"), /explicit administrator approval/);
  assert.match(emailStatusDetail("NEEDS_REVIEW"), /Delivery may have occurred/);
  assert.match(emailStatusDetail("FAILED"), /Review this failed attempt/);
});

test("email rows escape recipient text and never offer direct sends for bulk messages", () => {
  const html = emailTable([{ ...rows[1], recipient_email: "<script>recipient</script>", subject: "A & B" }], false, preview, { single: true, select: true });
  assert.match(html, /&lt;script&gt;recipient&lt;\/script&gt;/);
  assert.match(html, /A &amp; B/);
  assert.doesNotMatch(html, /email-send|<script>/);
});

test("registration prepares drafts and exposes bulk import without changing the photo flow", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /Registration does not send email/);
  assert.match(source, /showEmployeeImport\(context, load\)/);
  assert.match(source, /const photoInput = bindEmployeePhoto\(dialog, context\.max_photo_bytes\)/);
  assert.match(source, /photoInput\.dispose\(\)/);
  assert.doesNotMatch(source, /queues its email automatically|QR email queued|Queue email resend/);
});

test("queued individual recovery renders Resume send and outdated approval settings fail closed", () => {
  const queued = { ...rows[2], delivery_mode: "SINGLE" };
  const html = emailTable([queued], true, { ...gmail, sending_enabled: true }, { single: true });
  assert.match(html, /data-action="email-send"/);
  assert.match(html, /Resume send/);
  assert.match(html, /same email ID/);
  assert.throws(() => emailDeliverySettings({ ...gmail, approval_required: undefined }), /email approval migration/);
});

test("bulk batch review link and loaded-row selection stay explicit", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  const importer = await readFile(new URL("../src/employee_import.js", import.meta.url), "utf8");
  assert.match(source, /bulk_batch_id: batchId/);
  assert.match(source, /Select pending shown/);
  assert.match(source, /Select approved shown/);
  assert.match(source, /ids\.length > 100/);
  assert.match(source, /Approve and send/);
  assert.match(source, /operation\.approveAndProcess/);
  assert.match(source, /operation\.setProcessBatchSize\(settings\.process_batch_size\)/);
  assert.match(importer, /#emails\?bulk_batch_id=/);
});
