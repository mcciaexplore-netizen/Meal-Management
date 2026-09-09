import assert from "node:assert/strict";
import test from "node:test";
import { approvedReceipt, qrImageFile, scanDetails, servingLocations, servingScanners } from "../src/scan_helpers.js";
import { clearServingMarker, loadServingMarker, saveServingMarker } from "../src/scan_state.js";
import { developmentAccess, scenarioLabel } from "../src/development.js";
import { isFinalScanResult, randomIdentifier, ServingOperation } from "../src/core.js";

const catalog = {
  locations: [{ id: 1, is_active: true }, { id: 2, is_active: true }, { id: 3, is_active: false }],
  scanners: [{ code: "A", location_id: 1, is_active: true }, { code: "B", location_id: 2, is_active: true }, { code: "C", location_id: 1, is_active: false }, { code: "D", location_id: 3, is_active: true }],
  meal_types: [{ id: 1, is_active: true }, { id: 2, is_active: false }]
};
const requestId = "10000000-0000-4000-8000-000000000001";
const details = { token: "test-credential", location_id: 1, scanner_code: "A", meal_type_id: 1, category: "EMPLOYEE" };
const approval = { id: 7, waiter_id: 20, location_id: 1, scanner_code: "A", meal_type_id: 1, request_id: requestId, quantity: 3 };

test("location selection excludes inactive locations and unrelated or inactive counters", () => {
  assert.deepEqual(servingLocations(catalog).map(row => row.id), [1, 2]);
  assert.deepEqual(servingScanners(catalog, 1).map(row => row.code), ["A"]);
  assert.deepEqual(servingScanners(catalog, 3), []);
});

test("employee scans use a registered counter without client staff or location assertions", () => {
  assert.deepEqual(scanDetails(details, catalog, [], 20), { token: "test-credential", scanner_code: "A", meal_type_id: 1, quantity: 1 });
  assert.throws(() => scanDetails({ ...details, scanner_code: "B" }, catalog, [], 20), /selected location/);
  assert.throws(() => scanDetails({ ...details, meal_type_id: 2 }, catalog, [], 20), /active meal type/);
});

test("master scans retain the approved caller, request, quantity, and service scope", () => {
  const body = scanDetails({ ...details, category: "MASTER", authorization_id: 7 }, catalog, [approval], 20);
  assert.equal(body.request_id, requestId);
  assert.equal(body.quantity, 3);
  assert.equal(body.authorization_id, 7);
  assert.throws(() => scanDetails({ ...details, category: "MASTER", authorization_id: 7 }, catalog, [approval], 21), /assigned to your account/);
  assert.throws(() => scanDetails({ ...details, location_id: 2, scanner_code: "B", category: "MASTER", authorization_id: 7 }, catalog, [approval], 20), /exact location/);
});

test("image uploads validate formats and size before decoding", () => {
  assert.equal(qrImageFile({ type: "image/svg+xml", size: 100 }).size, 100);
  assert.throws(() => qrImageFile({ type: "text/html", size: 100 }), /PNG/);
  assert.throws(() => qrImageFile({ type: "image/png", size: 11 * 1024 * 1024 }), /10 MB/);
  assert.throws(() => qrImageFile({ type: "image/png", size: 0 }), /Choose/);
});

test("receipt failure cannot change a committed approval", async () => {
  const result = Object.freeze({ approved: true, request_id: requestId, serving_id: 1 });
  const loaded = await approvedReceipt(result, async () => { throw new Error("Unavailable"); });
  assert.equal(result.approved, true);
  assert.deepEqual(loaded, { receipt: null, unavailable: true });
});

test("rejected scans never request employee details", async () => {
  let calls = 0;
  await approvedReceipt({ approved: false, request_id: requestId }, async () => { calls += 1; });
  assert.equal(calls, 0);
});

function storage() {
  const values = new Map();
  return { values, setItem: (key, value) => values.set(key, value), getItem: key => values.get(key) ?? null, removeItem: key => values.delete(key) };
}

test("reload markers persist only opaque operation and service metadata", () => {
  const store = storage();
  const request = { request_id: requestId, token: "never-store-this", scanner_code: "A", meal_type_id: 1, quantity: 1, employee_name: "Never Store Name", password: "Never Store Password" };
  assert.equal(saveServingMarker(store, 20, request, 1), true);
  const serialized = [...store.values.values()][0];
  assert.doesNotMatch(serialized, /never-store|Never Store|token|password|employee_name/);
  assert.deepEqual(loadServingMarker(store, 20), { request: { request_id: requestId, scanner_code: "A", meal_type_id: 1, quantity: 1 }, location_id: 1 });
  assert.equal(loadServingMarker(store, 21), null);
  clearServingMarker(store, 20);
  assert.equal(loadServingMarker(store, 20), null);
});

test("marker loading rejects corrupt data and unavailable browser storage", () => {
  const store = storage();
  store.setItem("meal-office:serving:20", '{"request":{"request_id":"not-a-uuid"},"location_id":1}');
  assert.equal(loadServingMarker(store, 20), null);
  assert.equal(loadServingMarker(null, 20), null);
  assert.equal(saveServingMarker(null, 20, {}, 1), false);
});

test("restored operations accept only a fresh token and retain all original serving details", () => {
  const operation = new ServingOperation(() => "must-not-be-used");
  operation.restore({ request_id: requestId, scanner_code: "A", meal_type_id: 1, quantity: 3, authorization_id: 7 });
  const retried = operation.bind({ token: "rescanned-original", request_id: "wrong", scanner_code: "B", meal_type_id: 2, quantity: 20 });
  assert.deepEqual(retried, { request_id: requestId, scanner_code: "A", meal_type_id: 1, quantity: 3, authorization_id: 7, token: "rescanned-original" });
});

test("mismatched or unconfirmed HTTP200 results do not finalize a serving", async () => {
  for (const code of ["IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH", "PROCESSING_UNCONFIRMED", "SCAN_RECEIPT_UNCONFIRMED"]) {
    const operation = new ServingOperation(() => requestId);
    const result = await operation.submit(async () => ({ approved: false, code, request_id: requestId }), { token: "attempt", scanner_code: "A", meal_type_id: 1, quantity: 1 });
    assert.equal(operation.result, null);
    assert.equal(isFinalScanResult(result, operation.request), false);
    operation.forgetCredential();
    assert.equal(operation.request.request_id, requestId);
    assert.equal(operation.request.token, undefined);
    await operation.submit(async body => ({ approved: true, code: "APPROVED", request_id: body.request_id, serving_id: 1, meal_ids: [1], duplicate: true }), { token: "original" });
    assert.equal(operation.result.approved, true);
    assert.equal(operation.request.request_id, requestId);
  }
});

test("business rejections are finalized and preserve their original reason", async () => {
  const operation = new ServingOperation(() => requestId);
  await operation.submit(async () => ({ approved: false, code: "QR_EXPIRED", request_id: requestId }), { token: "expired" });
  assert.equal(operation.result.code, "QR_EXPIRED");
});

test("approvals require committed identifiers, the original request, and every expected meal", async () => {
  const valid = { approved: true, code: "APPROVED", request_id: requestId, serving_id: 10, meal_ids: [1, 2] };
  const request = { token: "test-credential", request_id: requestId, scanner_code: "A", meal_type_id: 1, quantity: 2 };
  const invalid = [
    { approved: true },
    { ...valid, request_id: "another-request" },
    { ...valid, code: "UNKNOWN_QR" },
    { ...valid, serving_id: 0 },
    { ...valid, meal_ids: [] },
    { ...valid, meal_ids: [1] },
    { ...valid, meal_ids: [1, 1] },
    { ...valid, meal_ids: [1, -2] },
    { ...valid, meal_ids: [1, "2"] },
    { approved: false, code: "QR_EXPIRED", request_id: "another-request" },
    { approved: false, code: "APPROVED", request_id: requestId },
    { approved: false, request_id: requestId }
  ];
  for (const response of invalid) {
    const operation = new ServingOperation();
    await operation.submit(async () => response, request);
    assert.equal(operation.result, null);
    assert.equal(operation.accept(response), false);
    assert.equal(operation.request.request_id, requestId);
  }
  const operation = new ServingOperation();
  await operation.submit(async () => valid, request);
  assert.deepEqual(operation.result, valid);
});

test("secure UUID fallback works when randomUUID is unavailable on a local network origin", () => {
  const random = { getRandomValues(bytes) { bytes.fill(255); return bytes; } };
  assert.equal(randomIdentifier(random), "ffffffff-ffff-4fff-bfff-ffffffffffff");
  assert.throws(() => randomIdentifier({}), /secure serving identifier/);
});

test("development gallery requires an admin and an explicit development flag", () => {
  assert.equal(developmentAccess({ roles: ["ADMIN"] }, { development_test_data: true }), true);
  assert.equal(developmentAccess({ roles: ["WAITER"] }, { development_test_data: true }), false);
  assert.equal(developmentAccess({ roles: ["ADMIN"] }, { development_test_data: "true" }), false);
  assert.equal(developmentAccess({ roles: ["ADMIN"] }, {}), false);
});

test("development invalid QR cases are labeled intentional", () => {
  assert.equal(scenarioLabel({ expected_code: "APPROVED" }).tone, "positive");
  for (const expected_code of ["QR_EXPIRED", "QR_REVOKED", "EMPLOYEE_INACTIVE"]) assert.match(scenarioLabel({ expected_code }).expected, /Intentional rejection/);
});
