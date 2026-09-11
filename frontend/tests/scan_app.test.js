import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { ScanAppOperation, scanReadBody } from "../src/scan_app_operation.js";
import { loadScannerMarker, saveScannerMarker } from "../src/scanner_state.js";
import { mealTable } from "../src/screens.js";

const requestId = "10000000-0000-4000-8000-000000000001";
const scope = "a".repeat(64);
const visitor = { company_name: "Test Company", name: "Test Visitor", email: "visitor@example.com", phone: "+91 98765 43210" };
const approved = { approved: true, code: "APPROVED", request_id: requestId, serving_id: 10, meal_ids: [12], duplicate: false };

test("scan-only HTML loads its own entry without dashboard navigation", async () => {
  const html = await readFile(new URL("../scan.html", import.meta.url), "utf8");
  assert.match(html, /src="\/assets\/scan-app\.js"/);
  assert.match(html, /id="scan-app"/);
  assert.doesNotMatch(html, /src="\/assets\/app\.js"|dashboard|sidebar|data-page/);
});

test("scanner animates final decisions and automatically resets after two seconds", async () => {
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/scan_only.css", import.meta.url), "utf8");
  assert.match(source, /Accepted · 1 meal/);
  assert.match(source, /Meal rejected/);
  assert.match(source, /QR has expired\. Please contact the administrator\./);
  assert.match(source, /scheduleScanResultReset\(/);
  assert.match(source, /clearMarker: \(\) => !markerSaved \|\| clearScannerMarker\(storage, servingScope\)/);
  assert.doesNotMatch(source, /visitor-meal-form|Scan next meal|\/visitors/);
  assert.match(styles, /@keyframes scan-result-arrive/);
  assert.match(styles, /@keyframes scan-result-icon/);
});

test("employee QR read sends no category or submitted staff identity and accepts one committed meal", async () => {
  const flow = new ScanAppOperation(() => requestId);
  let submitted;
  const result = await flow.read("employee-token", async body => { submitted = body; return approved; });
  assert.deepEqual(submitted, { request_id: requestId, token: "employee-token" });
  assert.equal(result.approved, true);
  assert.equal(flow.request.quantity, 1);
  assert.equal(flow.result.serving_id, 10);
});

test("master QR scan records one committed meal without visitor input", async () => {
  const flow = new ScanAppOperation(() => requestId);
  let submitted;
  await flow.read("master-token", async body => { submitted = body; return approved; });
  assert.deepEqual(submitted, { request_id: requestId, token: "master-token" });
  assert.equal(flow.result.approved, true);
});

test("parallel camera reads share one request and only one submission", async () => {
  let finish;
  let calls = 0;
  const flow = new ScanAppOperation(() => requestId);
  const send = async () => { calls += 1; return new Promise(resolve => { finish = resolve; }); };
  const first = flow.read("employee-token", send);
  assert.equal(await flow.read("employee-token", send), null);
  finish(approved);
  await first;
  assert.equal(calls, 1);
});

test("completed employee retries return the original committed result without another meal", async () => {
  let calls = 0;
  const flow = new ScanAppOperation(() => requestId);
  const send = async () => { calls += 1; return approved; };
  await flow.read("employee-token", send);
  const duplicate = await flow.read("employee-token", send);
  assert.equal(calls, 1);
  assert.equal(duplicate.serving_id, approved.serving_id);
  assert.equal(duplicate.duplicate, true);
});

test("master network retries retain the original request and credential", async () => {
  const flow = new ScanAppOperation(() => requestId);
  let firstBody;
  await assert.rejects(flow.read("master-token", async body => { firstBody = body; throw new Error("Unknown network outcome"); }));
  let retried;
  await flow.retry(async body => { retried = body; return approved; });
  assert.deepEqual(retried, firstBody);
  assert.equal(flow.result.serving_id, 10);
});

test("reload marker for master scan excludes credential and visitor data", async () => {
  const flow = new ScanAppOperation(() => requestId);
  await assert.rejects(flow.read("master-token", async () => { throw new Error("Unknown network outcome"); }));
  const values = new Map();
  const storage = { setItem: (key, value) => values.set(key, value), getItem: key => values.get(key) };
  saveScannerMarker(storage, scope, flow.request);
  const raw = [...values.values()][0];
  assert.doesNotMatch(raw, /master-token|company_name|visitor_details|Test Visitor|visitor@example|98765|phone/);
  const restored = new ScanAppOperation();
  restored.restore(loadScannerMarker(storage, scope));
  assert.equal(restored.request.request_id, requestId);
  assert.equal(restored.request.token, undefined);
});

test("staff-bound serving markers are not accepted by the public scanner", () => {
  const flow = new ScanAppOperation();
  assert.throws(() => flow.restore({ request_id: requestId, scanner_code: "COUNTER-A", meal_type_id: 1, quantity: 1 }), /different scanner workflow/);
  assert.throws(() => flow.restore({ request_id: requestId, quantity: 3, authorization_id: 7 }), /different scanner workflow/);
  assert.equal(flow.request, null);
});

test("read payload excludes quantity, visitor details, and caller-supplied identity", () => {
  const request = { request_id: requestId, token: "token", scanner_code: "A", meal_type_id: 1, quantity: 7, visitor_details: visitor, staff_id: 20, role: "ADMIN" };
  assert.deepEqual(Object.keys(scanReadBody(request)).sort(), ["request_id", "token"]);
});

test("dashboard meal reports show the four visitor fields with HTML escaping", () => {
  const html = mealTable([{ id: 1, kind: "MASTER", visitor_company_name: "A & B", visitor_name: "Visitor <Name>", visitor_email: "visitor@example.com", visitor_phone: "+91 98765 43210", meal_type_name: "Lunch", waiter_name: "Staff", location_name: "Office", served_at: "2026-09-09T12:00:00Z" }]);
  assert.match(html, /A &amp; B/);
  assert.match(html, /Visitor &lt;Name&gt;/);
  assert.match(html, /visitor@example.com/);
  assert.match(html, /\+91 98765 43210/);
  assert.doesNotMatch(html, /Visitor <Name>/);
});

test("dashboard meal reports show employee company email and phone", () => {
  const html = mealTable([{ id: 2, kind: "EMPLOYEE", employee_code: "EMP-2", employee_name: "Employee Name", employee_company_name: "Employee Company", employee_email: "employee@example.com", employee_phone: "+91 98765 43211", meal_type_name: "Lunch", waiter_name: "Scanner", location_name: "Office", served_at: "2026-09-09T12:00:00Z" }]);
  assert.match(html, /Employee Company/);
  assert.match(html, /employee@example.com/);
  assert.match(html, /\+91 98765 43211/);
});

test("administrator meal history can render a removal control", () => {
  const html = mealTable([{ id: 19, kind: "EMPLOYEE", employee_name: "Asha", meal_type_name: "Lunch", waiter_name: "Scanner", location_name: "Office", served_at: "2026-09-09T12:00:00Z" }], true);
  assert.match(html, /data-action="meal-remove"/);
  assert.match(html, /data-id="19"/);
  assert.match(html, />Remove</);
});

test("missing master details remain unconfirmed instead of permitting a new serving", async () => {
  const flow = new ScanAppOperation(() => requestId);
  await flow.read("master-token", async () => ({ approved: false, code: "VISITOR_DETAILS_REQUIRED", request_id: requestId }));
  assert.equal(flow.result, null);
});

test("a restored serving with a mismatched QR stays unresolved until its original result is recovered", async () => {
  const flow = new ScanAppOperation();
  flow.restore({ request_id: requestId, quantity: 1 });
  const result = await flow.read("wrong-token", async () => ({ approved: false, code: "REQUEST_MISMATCH", request_id: requestId }));
  assert.equal(flow.accept(result), false);
  assert.equal(flow.result, null);
  assert.throws(() => flow.reset(), /Confirm the current serving/);
  flow.forgetCredential();
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.request.token, undefined);
  assert.equal(flow.accept(approved), true);
  flow.reset();
  assert.equal(flow.request, null);
});

test("an invalid QR for a fresh serving remains a final rejection", async () => {
  const flow = new ScanAppOperation(() => requestId);
  await flow.read("invalid-token", async () => ({ approved: false, code: "INVALID_QR", request_id: requestId }));
  assert.equal(flow.result.code, "INVALID_QR");
  flow.reset();
  assert.equal(flow.request, null);
  assert.equal(flow.readStarted, false);
});

test("a restored request cannot finalize an invalid QR while its original request may still be arriving", async () => {
  const flow = new ScanAppOperation();
  flow.restore({ request_id: requestId, quantity: 1 });
  const result = await flow.read("invalid-token", async () => ({ approved: false, code: "INVALID_QR", request_id: requestId }));
  assert.equal(result.code, "REQUEST_MISMATCH");
  assert.equal(flow.result, null);
  assert.equal(flow.accept(result), false);
  assert.throws(() => flow.reset(), /Confirm the current serving/);
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.accept(approved), true);
});

test("an uncertain first submission cannot become a final invalid QR rejection on retry", async () => {
  const flow = new ScanAppOperation(() => requestId);
  await assert.rejects(flow.read("employee-token", async () => {
    assert.equal(flow.readStarted, true);
    throw new Error("Connection interrupted before the original request arrived");
  }));
  const result = await flow.retry(async () => ({ approved: false, code: "INVALID_QR", request_id: requestId }), async () => { throw new Error("Unexpected visitor submission"); });
  assert.equal(result.code, "REQUEST_MISMATCH");
  assert.equal(flow.result, null);
  assert.throws(() => flow.reset(), /Confirm the current serving/);
  flow.forgetCredential();
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.request.token, undefined);
});

test("the scanner entry opens directly without staff authentication or service selection", async () => {
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /auth\/|sign.?in|logout|login|catalog|station|selectField|meal_type_id|scanner_code|staff_id/);
  assert.match(source, /await openScanner\(\)/);
  assert.match(source, /Scanner unavailable/);
  assert.match(source, /Start camera/);
  assert.match(source, /loadActiveScannerMarker\(storage, session\.scope\)/);
  assert.match(source, /activeMarker\.scope !== session\.scope/);
});
