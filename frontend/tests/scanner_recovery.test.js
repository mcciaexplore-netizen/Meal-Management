import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { ScanAppOperation } from "../src/scan_app_operation.js";
import { chooseScannerRecovery, recoveryMarker } from "../src/scanner_recovery.js";
import { loadScannerMarker, saveScannerMarker } from "../src/scanner_state.js";

const requestId = "10000000-0000-4000-8000-000000000001";
const request = { request_id: requestId, quantity: 1 };

test("startup with no browser marker recovers only the existing request identity", async () => {
  let lookups = 0;
  const recovered = await chooseScannerRecovery({ recover: async () => { lookups += 1; return { request }; } });
  assert.equal(lookups, 1);
  assert.deepEqual(recovered, { request, fromServer: true });
  assert.deepEqual(Object.keys(recovered.request).sort(), ["quantity", "request_id"]);
});

test("a local pending marker avoids the server fallback", async () => {
  const recovered = await chooseScannerRecovery({ localMarker: request, recover: async () => { throw new Error("Unexpected lookup"); } });
  assert.deepEqual(recovered, { request, fromServer: false });
});

test("an in-memory serving takes priority over both a local marker and server lookup", async () => {
  const recovered = await chooseScannerRecovery({
    currentRequest: { ...request, token: "private-credential" },
    localMarker: { request_id: "20000000-0000-4000-8000-000000000002", quantity: 1 },
    recover: async () => { throw new Error("Unexpected lookup"); }
  });
  assert.deepEqual(recovered, { request: null, fromServer: false });
});

test("only an explicit empty recovery response allows startup without a previous serving", async () => {
  assert.deepEqual(await chooseScannerRecovery({ recover: async () => ({ request: null }) }), { request: null, fromServer: false });
});

test("recovery network failures propagate and never become permission to start a new meal", async () => {
  await assert.rejects(chooseScannerRecovery({ recover: async () => { throw new Error("Connection interrupted"); } }), /Connection interrupted/);
});

test("malformed or private-data-bearing server recovery responses are rejected", () => {
  const invalid = [
    undefined, null, [], {}, { request: undefined }, { request, extra: true },
    { request: [] }, { request: { ...request, quantity: 2 } },
    { request: { ...request, quantity: "1" } }, { request: { ...request, request_id: "employee-1" } },
    { request: { ...request, request_id: "00000000-0000-0000-0000-000000000000" } },
    { request: { ...request, token: "private-credential" } },
    { request: { ...request, visitor_details: { email: "private@example.test" } } },
    { request: { ...request, staff_id: 1 } }
  ];
  for (const response of invalid) assert.throws(() => recoveryMarker(response), /previous serving could not be verified/);
});

test("recovered port-transition requests persist no credential and cannot reset until their original result is known", async () => {
  const data = new Map();
  const storage = { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
  const scope = "b".repeat(64);
  const recovered = await chooseScannerRecovery({ recover: async () => ({ request }) });
  assert.equal(saveScannerMarker(storage, scope, recovered.request), true);
  assert.deepEqual(loadScannerMarker(storage, scope), request);
  const flow = new ScanAppOperation();
  flow.restore(recovered.request);
  assert.throws(() => flow.reset(), /Confirm the current serving/);
  assert.equal(flow.request.token, undefined);
  assert.equal(flow.request.visitor_details, undefined);
  assert.equal(flow.accept({ approved: true, code: "APPROVED", request_id: requestId, serving_id: 7, meal_ids: [9], duplicate: true }), true);
  assert.equal(flow.result.serving_id, 7);
  assert.equal(flow.request.request_id, requestId);
  flow.reset();
  assert.equal(flow.request, null);
});

test("scanner startup waits for scoped server recovery before rendering its controls", async () => {
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.match(source, /await chooseScannerRecovery/);
  assert.match(source, /transport\.request\("\/recovery", \{\}, session\.scope\)/);
  assert.match(source, /saveScannerMarker\(storage, servingScope, recovery\.request\)/);
  assert.match(source, /flow\.restore\(recovery\.request\)/);
  assert.ok(source.indexOf("await chooseScannerRecovery") < source.indexOf("    renderScanner();"));
});
