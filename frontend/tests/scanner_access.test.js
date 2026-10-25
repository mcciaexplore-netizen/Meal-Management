import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { activeDateRange, addMonths, displayActiveUntil, formatScannerCode, istDate, scannerCodePayload } from "../src/scanner_access.js";

test("scanner codes are grouped for reading aloud", () => {
  assert.equal(formatScannerCode("ABCDEFGH"), "ABCD-EFGH");
  assert.equal(formatScannerCode("abcd efgh"), "ABCD-EFGH");
  assert.equal(formatScannerCode("ABCD-EFGH"), "ABCD-EFGH");
});

test("today is decided by India time, not the browser or UTC date", () => {
  assert.equal(istDate(new Date("2026-10-25T18:29:59.999Z")), "2026-10-25");
  assert.equal(istDate(new Date("2026-10-25T18:30:00.000Z")), "2026-10-26");
  assert.equal(istDate(new Date("2026-12-31T20:00:00Z")), "2027-01-01");
});

test("active-until dates are limited to six calendar months ahead", () => {
  assert.equal(addMonths("2026-10-25", 6), "2027-04-25");
  assert.equal(addMonths("2026-08-31", 6), "2027-02-28");
  assert.equal(addMonths("2027-08-31", 6), "2028-02-29");
  assert.equal(addMonths("2026-12-31", 6), "2027-06-30");
  assert.deepEqual(activeDateRange(new Date("2026-10-25T10:00:00Z")), { min: "2026-10-25", max: "2027-04-25" });
});

test("code requests require a label and a date inside the allowed range", () => {
  const now = new Date("2026-10-25T10:00:00Z");
  assert.deepEqual(scannerCodePayload({ label: "  Counter 2 ", active_until: "2026-11-30" }, now), { label: "Counter 2", active_until: "2026-11-30" });
  assert.deepEqual(scannerCodePayload({ label: "Counter", active_until: "2027-04-25" }, now).active_until, "2027-04-25");
  for (const values of [
    { label: "", active_until: "2026-11-30" },
    { label: "x".repeat(101), active_until: "2026-11-30" },
    { label: "Counter", active_until: "2026-10-24" },
    { label: "Counter", active_until: "2027-04-26" },
    { label: "Counter", active_until: "30/11/2026" },
    { label: "Counter" }
  ]) {
    assert.throws(() => scannerCodePayload(values, now));
  }
});

test("active-until is shown as the end of the India calendar day", () => {
  assert.equal(displayActiveUntil({ active_until_date: null }), "Until revoked");
  const text = displayActiveUntil({ active_until_date: "2026-11-30" });
  assert.match(text, /^End of .*30.* \(IST\)$/);
  assert.match(text, /2026/);
});

test("admin workspace exposes the scanner access page with one-time codes and revocation", async () => {
  const app = await readFile(new URL("../src/app.js", import.meta.url), "utf8");
  assert.match(app, /\["scanner-access", \["Scanner access", "scan", scannerAccess\]\]/);
  const source = await readFile(new URL("../src/scanner_access.js", import.meta.url), "utf8");
  assert.match(source, /It is shown only once/);
  assert.match(source, /\/scanner-phones\/\$\{encodeURIComponent\(button\.dataset\.revoke\)\}\/revoke/);
  assert.match(source, /India time \(IST\)/);
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});

test("scanner activation accepts short admin codes", async () => {
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.match(source, /minlength="8"/);
  assert.match(source, /autocapitalize="characters"/);
  assert.doesNotMatch(source, /minlength="12"/);
});
