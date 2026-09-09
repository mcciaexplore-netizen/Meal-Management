import assert from "node:assert/strict";
import test from "node:test";
import { clearScannerMarker, loadActiveScannerMarker, loadScannerMarker, saveScannerMarker } from "../src/scanner_state.js";

const scope = "a".repeat(64);
const otherScope = "b".repeat(64);
const request = { request_id: "10000000-0000-4000-8000-000000000001", quantity: 1 };

function memoryStorage() {
  const values = new Map();
  return { values, get length() { return values.size; }, key: index => [...values.keys()][index] ?? null, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}

test("public scanner markers persist only UUID and one meal in a separate browser scope", () => {
  const storage = memoryStorage();
  saveScannerMarker(storage, scope, { ...request, token: "secret-token", staff_id: 4, scanner_code: "PRIVATE", meal_type_id: 7, visitor_details: { email: "private@example.com" } });
  assert.deepEqual(loadScannerMarker(storage, scope), request);
  assert.deepEqual([...storage.values.keys()], ["meal-office:public-scanner-active", `meal-office:public-scanner:${scope}`]);
  assert.equal(storage.getItem(`meal-office:public-scanner:${scope}`), JSON.stringify(request));
  assert.deepEqual(JSON.parse(storage.getItem("meal-office:public-scanner-active")), { scope, request });
  assert.equal(loadScannerMarker(storage, otherScope), null);
});

test("public scanner changes preserve old staff and other browser markers", () => {
  const storage = memoryStorage();
  storage.setItem("meal-office:serving:10", "legacy-request");
  saveScannerMarker(storage, otherScope, request);
  saveScannerMarker(storage, scope, request);
  assert.equal(clearScannerMarker(storage, scope), true);
  assert.equal(storage.getItem("meal-office:serving:10"), "legacy-request");
  assert.deepEqual(loadScannerMarker(storage, otherScope), request);
  assert.equal(loadScannerMarker(storage, scope), null);
});

test("invalid or unavailable browser marker storage fails without storing credentials", () => {
  const storage = memoryStorage();
  assert.equal(saveScannerMarker(storage, "invalid-scope", request), false);
  assert.equal(saveScannerMarker(storage, scope, { ...request, quantity: 2 }), false);
  assert.equal(saveScannerMarker(storage, scope, { ...request, request_id: "invalid" }), false);
  assert.equal(saveScannerMarker(null, scope, request), false);
  assert.equal(loadScannerMarker(null, scope), null);
  assert.equal(clearScannerMarker(null, scope), false);
  assert.equal(storage.values.size, 0);
});

test("reload after cookie reset finds the pending serving in the previous browser scope", () => {
  const storage = memoryStorage();
  saveScannerMarker(storage, scope, request);
  assert.equal(loadScannerMarker(storage, otherScope), null);
  assert.deepEqual(loadActiveScannerMarker(storage, otherScope), { scope, request });
  assert.deepEqual(loadScannerMarker(storage, scope), request);
  assert.deepEqual(loadActiveScannerMarker(storage, scope), { scope, request });
});

test("older scoped markers are detected after reload without reading old staff marker data", () => {
  const storage = memoryStorage();
  storage.setItem("meal-office:serving:10", "legacy-private-request");
  storage.setItem(`meal-office:public-scanner:${scope}`, JSON.stringify(request));
  const originalGet = storage.getItem;
  storage.getItem = key => {
    assert.notEqual(key, "meal-office:serving:10");
    return originalGet(key);
  };
  assert.deepEqual(loadActiveScannerMarker(storage, otherScope), { scope, request });
  assert.equal(storage.values.get("meal-office:serving:10"), "legacy-private-request");
});

test("known completion clears its active pointer and never clears a foreign pending pointer", () => {
  const storage = memoryStorage();
  saveScannerMarker(storage, scope, request);
  assert.equal(clearScannerMarker(storage, scope), true);
  assert.equal(loadActiveScannerMarker(storage, otherScope), null);
  saveScannerMarker(storage, scope, request);
  assert.equal(clearScannerMarker(storage, otherScope), true);
  assert.deepEqual(loadActiveScannerMarker(storage, otherScope), { scope, request });
});

test("partial storage writes retain enough information to recover the serving", () => {
  for (const blockedKey of ["meal-office:public-scanner-active", `meal-office:public-scanner:${scope}`]) {
    const storage = memoryStorage();
    const originalSet = storage.setItem;
    storage.setItem = (key, value) => {
      if (key === blockedKey) throw new Error("Storage unavailable");
      originalSet(key, value);
    };
    assert.equal(saveScannerMarker(storage, scope, request), true);
    assert.deepEqual(loadActiveScannerMarker(storage, otherScope), { scope, request });
  }
});
