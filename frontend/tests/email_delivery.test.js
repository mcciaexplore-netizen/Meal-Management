import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { canPreviewEmail, emailDeliverySettings, emailModeDescription, emailModeLabel, emailStatusDetail } from "../src/email_delivery.js";
import { emailTable } from "../src/screens.js";

const preview = { backend: "preview", sending_enabled: false, preview_available: true };
const gmail = { backend: "gmail", sending_enabled: false, preview_available: false };
const rows = ["QUEUED", "CANCELLED", "SENT", "FAILED"].map((status, index) => ({ id: index + 21, recipient_email: "employee@example.test", status, created_at: "2026-09-09T10:00:00Z" }));

test("preview mode explicitly states that no messages are sent", () => {
  assert.match(emailModeLabel(preview), /Local previews only; no emails sent/);
  assert.match(emailModeDescription(preview), /does not deliver messages/);
  assert.match(emailModeDescription(preview), /leaves the message in the queue/);
});

test("Gmail and SES distinguish disabled delivery from manual-only delivery", () => {
  for (const backend of ["gmail", "ses"]) {
    for (const enabled of [false, true]) {
      const settings = { backend, sending_enabled: enabled, preview_available: false };
      assert.match(emailModeLabel(settings), enabled ? /sending enabled/ : /sending disabled/);
      assert.match(emailModeLabel(settings), backend === "gmail" ? /Gmail/ : /Amazon SES/);
      assert.match(emailModeDescription(settings), enabled ? /approved manual send/ : /delivery is disabled/);
      assert.doesNotMatch(emailModeDescription(settings), /for automatic delivery/);
    }
  }
});

test("automatic delivery reports the running sender and configured polling interval", () => {
  for (const backend of ["gmail", "ses"]) {
    const settings = emailDeliverySettings({ backend, sending_enabled: true, preview_available: false, automatic_enabled: true, worker_running: true, poll_seconds: 7, batch_size: 4 });
    assert.match(emailModeLabel(settings), /automatic delivery running/);
    assert.match(emailModeDescription(settings), /Registration and QR resends/);
    assert.match(emailModeDescription(settings), /every 7 seconds/);
    assert.match(emailModeDescription(settings), /Refresh this list/);
    assert.doesNotMatch(emailModeDescription(settings), /manual send/);
  }
});

test("configured automation without a running sender tells administrators that emails wait", () => {
  const settings = { ...gmail, sending_enabled: true, automatic_enabled: true, worker_running: false };
  assert.match(emailModeLabel(settings), /automatic delivery not running/);
  assert.match(emailModeDescription(settings), /sender is not running/);
  assert.match(emailModeDescription(settings), /Queued emails will wait/);
});

test("invalid or contradictory automation settings fail without claiming delivery is running", () => {
  for (const value of [
    { ...gmail, automatic_enabled: "true" },
    { ...gmail, automatic_enabled: true },
    { ...gmail, worker_running: true },
    { ...preview, sending_enabled: true, automatic_enabled: true },
    { ...gmail, poll_seconds: 0 },
    { ...gmail, poll_seconds: 301 },
    { ...gmail, poll_seconds: "5" },
    { ...gmail, batch_size: 0 },
    { ...gmail, batch_size: 101 },
  ]) assert.throws(() => emailDeliverySettings(value), /could not be confirmed/);
});

test("only a queued message in an available local preview mode can expose Preview", () => {
  assert.equal(canPreviewEmail(rows[0], preview), true);
  for (const row of rows.slice(1)) assert.equal(canPreviewEmail(row, preview), false);
  for (const settings of [gmail, { ...preview, preview_available: false }, null]) {
    for (const row of rows) assert.equal(canPreviewEmail(row, settings), false);
  }
});

test("full and compact tables show queue IDs and omit unavailable preview actions", () => {
  for (const compact of [false, true]) {
    const local = emailTable(rows, compact, preview);
    assert.equal((local.match(/data-action="email-preview"/g) ?? []).length, 1);
    assert.match(local, /data-id="21"/);
    assert.match(local, /Queue ID/);
    assert.match(local, /class="mono">24</);
    const remote = emailTable(rows, compact, gmail);
    assert.doesNotMatch(remote, /email-preview|>Preview</);
    assert.match(remote, /Queue ID/);
  }
});

test("Sent describes provider acceptance while Failed requests review", () => {
  assert.match(emailStatusDetail("SENT"), /Accepted by the email provider/);
  assert.match(emailStatusDetail("SENT"), /inbox delivery is not confirmed/);
  assert.match(emailStatusDetail("FAILED"), /Review this attempt before retrying/);
  const html = emailTable(rows, false, gmail);
  assert.match(html, /inbox delivery is not confirmed/);
  assert.match(html, /Review this attempt before retrying/);
});

test("email row display escapes recipient text and never creates a sending action", () => {
  const html = emailTable([{ ...rows[0], recipient_email: "<script>recipient</script>", subject: "A & B" }], false, preview);
  assert.match(html, /&lt;script&gt;recipient&lt;\/script&gt;/);
  assert.match(html, /A &amp; B/);
  assert.doesNotMatch(html, /data-action="(?:send|email-send)|<script>/);
});

test("invalid delivery settings fail safely instead of implying previews are available", () => {
  for (const value of [null, {}, { ...gmail, backend: "unknown" }, { ...gmail, preview_available: true }, { ...gmail, sending_enabled: "true" }]) assert.throws(() => emailDeliverySettings(value), /could not be confirmed/);
  assert.deepEqual(emailDeliverySettings({ ...gmail, sender: "private@example.test" }), { ...gmail, automatic_enabled: false, worker_running: false, poll_seconds: 5, batch_size: 10 });
});

test("employee details and email queue load safe email settings without adding sending endpoints", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /emailTable\(list\(queue\), true, emailSettings\)/);
  assert.equal((source.match(/api\("\/email-settings"\)/g) ?? []).length, 2);
  assert.doesNotMatch(source, /api\([^\n]*email[^\n]*\/send/);
});
