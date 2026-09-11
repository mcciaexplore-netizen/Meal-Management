import assert from "node:assert/strict";
import test from "node:test";
import { ScanAppOperation } from "../src/scan_app_operation.js";
import { scheduleScanResultReset } from "../src/scan_result_reset.js";

const requestId = "10000000-0000-4000-8000-000000000001";
const approved = { approved: true, code: "APPROVED", request_id: requestId, serving_id: 10, meal_ids: [12], duplicate: false };
const rejected = { approved: false, code: "QR_EXPIRED", request_id: requestId };

function fixture(context) {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const flow = new ScanAppOperation(() => requestId);
  flow.bind("private-qr-token");
  const events = [];
  const callbacks = {
    flow,
    active: () => true,
    clearMarker: () => { events.push("clear"); return true; },
    onReset: () => events.push("reset"),
    onBlocked: () => events.push("blocked")
  };
  return { flow, events, callbacks };
}

for (const [label, result] of [["approval", approved], ["rejection", rejected]]) {
  test(`confirmed ${label} remains visible for two seconds before returning to capture`, context => {
    const { flow, events, callbacks } = fixture(context);
    assert.equal(flow.accept(result), true);
    scheduleScanResultReset(callbacks);
    context.mock.timers.tick(1999);
    assert.equal(flow.result, result);
    assert.equal(flow.request.request_id, requestId);
    assert.deepEqual(events, []);
    context.mock.timers.tick(1);
    assert.equal(flow.result, null);
    assert.equal(flow.request, null);
    assert.deepEqual(events, ["clear", "reset"]);
    context.mock.timers.tick(10000);
    assert.deepEqual(events, ["clear", "reset"]);
  });
}

test("an unconfirmed result retains the request and does not schedule a new serving", context => {
  const { flow, events, callbacks } = fixture(context);
  scheduleScanResultReset(callbacks);
  context.mock.timers.tick(20000);
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.result, null);
  assert.deepEqual(events, []);
});

test("a failed recovery-marker removal keeps the committed result until clearing can be retried", context => {
  const { flow, events, callbacks } = fixture(context);
  flow.accept(approved);
  scheduleScanResultReset({ ...callbacks, clearMarker: () => false });
  context.mock.timers.tick(2000);
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.result, approved);
  assert.deepEqual(events, ["blocked"]);
  scheduleScanResultReset(callbacks);
  context.mock.timers.tick(2000);
  assert.equal(flow.request, null);
  assert.deepEqual(events, ["blocked", "clear", "reset"]);
});

test("a storage exception cannot discard the completed serving", context => {
  const { flow, events, callbacks } = fixture(context);
  flow.accept(approved);
  scheduleScanResultReset({ ...callbacks, clearMarker: () => { throw new Error("Storage blocked"); } });
  context.mock.timers.tick(2000);
  assert.equal(flow.request.request_id, requestId);
  assert.equal(flow.result, approved);
  assert.deepEqual(events, ["blocked"]);
});

test("leaving the scanner cancels the delayed screen update", context => {
  const { flow, events, callbacks } = fixture(context);
  flow.accept(approved);
  const cancel = scheduleScanResultReset(callbacks);
  context.mock.timers.tick(1000);
  cancel();
  context.mock.timers.tick(10000);
  assert.equal(flow.result, approved);
  assert.deepEqual(events, []);
});

test("an inactive page cannot clear the serving through an expired callback", context => {
  const { flow, events, callbacks } = fixture(context);
  flow.accept(approved);
  scheduleScanResultReset({ ...callbacks, active: () => false });
  context.mock.timers.tick(2000);
  assert.equal(flow.result, approved);
  assert.deepEqual(events, []);
});

test("an old timer cannot discard a different current serving", context => {
  const { flow, events, callbacks } = fixture(context);
  flow.accept(approved);
  scheduleScanResultReset(callbacks);
  flow.reset();
  flow.bind("another-qr-token");
  context.mock.timers.tick(2000);
  assert.equal(flow.request.token, "another-qr-token");
  assert.deepEqual(events, []);
});

test("a lost response cannot advance before its committed result is recovered", async context => {
  const { flow, events, callbacks } = fixture(context);
  await assert.rejects(flow.read("private-qr-token", async () => { throw new Error("Connection interrupted"); }));
  scheduleScanResultReset(callbacks);
  context.mock.timers.tick(20000);
  assert.equal(flow.request.request_id, requestId);
  assert.deepEqual(events, []);
  flow.accept({ ...approved, duplicate: true });
  scheduleScanResultReset(callbacks);
  context.mock.timers.tick(2000);
  assert.equal(flow.request, null);
  assert.deepEqual(events, ["clear", "reset"]);
});
