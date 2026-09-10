import assert from "node:assert/strict";
import test from "node:test";
import { BulkEmailOperation, SingleEmailOperation, selectedEmailIds, validateEmailResult } from "../src/email_actions.js";

const draft = { id: 7, status: "DRAFT", delivery_mode: "SINGLE" };
const accepted = { email_id: 7, status: "SENT", code: null };
const bulkRows = (count, status = "QUEUED") => Array.from({ length: count }, (_, index) => ({ id: index + 1, status, delivery_mode: "BULK", approved_at: status === "QUEUED" ? "2026-09-10T00:00:00Z" : null }));
const rejected = message => Promise.reject(new Error(message));

test("single send is explicit and only succeeds after a matching provider result", async () => {
  const calls = [];
  let resolve;
  const operation = new SingleEmailOperation(3, draft, (...args) => { calls.push(args); return new Promise(done => { resolve = done; }); });
  assert.equal(calls.length, 0);
  const pending = operation.send();
  assert.equal(operation.status, "DRAFT");
  assert.equal(operation.inFlight, true);
  assert.equal(operation.unknown, true);
  assert.equal(await operation.send(), null);
  resolve(accepted);
  assert.deepEqual(await pending, accepted);
  assert.equal(operation.status, "SENT");
  assert.equal(operation.unknown, false);
  assert.deepEqual(calls, [["/employees/3/emails/7/send", { method: "POST" }]]);
  await assert.rejects(() => operation.send(), /Only an individual draft/);
});

test("unknown single delivery checks status and reuses the same email ID before retry", async () => {
  const calls = [];
  let phase = 0;
  const operation = new SingleEmailOperation(3, draft, (path, options) => {
    calls.push([path, options]);
    if (phase++ === 0) return rejected("disconnected");
    if (!options) return Promise.resolve({ email_id: 7, status: "DRAFT" });
    return Promise.resolve(accepted);
  });
  await assert.rejects(() => operation.send(), /result is not confirmed/);
  await assert.rejects(() => operation.send(), /Check this email's status/);
  assert.equal(calls.length, 1);
  await operation.check();
  await operation.send();
  assert.deepEqual(calls.map(row => row[0]), ["/employees/3/emails/7/send", "/employees/3/emails/7", "/employees/3/emails/7/send"]);
});

test("uncertain or failed delivery cannot automatically produce another send", async () => {
  for (const status of ["FAILED", "NEEDS_REVIEW", "CANCELLED", "ALREADY_SENT"]) {
    let calls = 0;
    const operation = new SingleEmailOperation(3, draft, async () => { calls += 1; return { email_id: 7, status }; });
    await operation.send();
    await assert.rejects(() => operation.send(), /Only an individual draft/);
    assert.equal(calls, 1);
  }
});

test("a single email interrupted after approval resumes the same queued ID", async () => {
  const calls = [];
  const operation = new SingleEmailOperation(3, draft, async (path, options) => {
    calls.push([path, options]);
    if (calls.length === 1) throw new Error("approval committed before disconnect");
    if (!options) return { id: 7, status: "QUEUED", approved_at: "2026-09-10T00:00:00Z" };
    return { email_id: 7, status: "ALREADY_SENT" };
  });
  await assert.rejects(() => operation.send(), /not confirmed/);
  await operation.check();
  assert.equal(operation.status, "QUEUED");
  await operation.send();
  assert.deepEqual(calls.map(row => row[0]), ["/employees/3/emails/7/send", "/employees/3/emails/7", "/employees/3/emails/7/send"]);
  assert.equal(operation.status, "ALREADY_SENT");
});

test("malformed and mismatched send responses stay unconfirmed", async () => {
  for (const value of [null, { status: "SENT" }, { ...accepted, email_id: 8 }, { ...accepted, status: "APPROVED" }]) {
    assert.throws(() => validateEmailResult(value, 7), /not confirmed/);
    const operation = new SingleEmailOperation(3, draft, async () => value);
    await assert.rejects(() => operation.send(), /not confirmed/);
    assert.equal(operation.unknown, true);
    assert.equal(operation.status, "DRAFT");
  }
});

test("a failed status check preserves uncertainty and never sends", async () => {
  const calls = [];
  const operation = new SingleEmailOperation(3, draft, async path => { calls.push(path); throw new Error("offline"); });
  await assert.rejects(() => operation.send());
  await assert.rejects(() => operation.check());
  assert.equal(operation.unknown, true);
  assert.equal(calls.length, 2);
  assert.match(calls[1], /emails\/7$/);
});

test("bulk selection requires valid explicit IDs and separate approval eligibility", () => {
  const rows = bulkRows(3, "PENDING_APPROVAL");
  assert.deepEqual(selectedEmailIds(rows, [3, 1], "approve"), [1, 3]);
  for (const selected of [[], [1, 1], [4], ["1"], Array.from({ length: 101 }, (_, index) => index + 1)]) assert.throws(() => selectedEmailIds(rows, selected, "approve"));
  assert.throws(() => selectedEmailIds(rows, [1], "process"), /only approved bulk/);
  assert.throws(() => selectedEmailIds([{ ...rows[0], delivery_mode: "SINGLE" }], [1], "approve"), /only bulk/);
});

test("approval sends only selected IDs and never starts processing", async () => {
  const calls = [];
  const operation = new BulkEmailOperation(async (path, options) => { calls.push([path, options]); return { email_ids: [1, 3], approved_count: 2 }; });
  await operation.approve(bulkRows(3, "PENDING_APPROVAL"), [3, 1]);
  assert.deepEqual(calls, [["/email-queue/approve", { method: "POST", body: { email_ids: [1, 3] } }]]);
});

test("unconfirmed approval never triggers processing", async () => {
  for (const response of [null, { email_ids: [1, 2] }, { email_ids: [1, 1] }]) {
    const calls = [];
    const operation = new BulkEmailOperation(async path => { calls.push(path); return response; });
    await assert.rejects(() => operation.approve(bulkRows(3, "PENDING_APPROVAL"), [1, 3]), /not confirmed/);
    assert.deepEqual(calls, ["/email-queue/approve"]);
  }
});

test("approved bulk processing uses sequential chunks of at most ten", async () => {
  const calls = [];
  let active = 0;
  const operation = new BulkEmailOperation(async (path, options) => {
    active += 1;
    assert.equal(active, 1);
    calls.push([path, options.body.email_ids]);
    await Promise.resolve();
    active -= 1;
    return { results: options.body.email_ids.map(email_id => ({ email_id, status: "SENT" })) };
  });
  const results = await operation.process(bulkRows(23), Array.from({ length: 23 }, (_, index) => index + 1));
  assert.equal(results.length, 23);
  assert.deepEqual(calls.map(row => row[1].length), [10, 10, 3]);
  assert.ok(calls.every(row => row[0] === "/email-queue/process"));
});

test("unapproved messages cannot reach the bulk processing endpoint", async () => {
  let calls = 0;
  const operation = new BulkEmailOperation(async () => { calls += 1; });
  for (const rows of [bulkRows(1, "PENDING_APPROVAL"), [{ ...bulkRows(1)[0], approved_at: null }], [{ ...bulkRows(1)[0], delivery_mode: "SINGLE" }]]) await assert.rejects(() => operation.process(rows, [1]), /only approved bulk/);
  assert.equal(calls, 0);
});

test("network uncertainty and malformed processing results stop later chunks", async () => {
  for (const outcome of [() => rejected("offline"), async () => ({ results: [] }), async () => ({ results: [{ email_id: 999, status: "SENT" }] })]) {
    let calls = 0;
    const operation = new BulkEmailOperation((...args) => { calls += 1; return outcome(...args); });
    await assert.rejects(() => operation.process(bulkRows(20), Array.from({ length: 20 }, (_, index) => index + 1)), /Processing stopped/);
    assert.equal(calls, 1);
    assert.equal(operation.unknown, true);
    assert.equal(operation.inFlight, false);
  }
});

test("a review outcome stops subsequent chunks without re-sending completed IDs", async () => {
  let calls = 0;
  const operation = new BulkEmailOperation(async (path, options) => {
    calls += 1;
    return { results: options.body.email_ids.map(email_id => ({ email_id, status: email_id === 1 ? "NEEDS_REVIEW" : "SENT" })) };
  });
  const results = await operation.process(bulkRows(20), Array.from({ length: 20 }, (_, index) => index + 1));
  assert.equal(results.length, 10);
  assert.equal(calls, 1);
});

test("one Approve and send action waits for confirmed exact approval before processing only that selection", async () => {
  const calls = [];
  let confirmApproval;
  const operation = new BulkEmailOperation(async (path, options) => {
    calls.push([path, options.body.email_ids]);
    if (path === "/email-queue/approve") return new Promise(resolve => { confirmApproval = resolve; });
    return { results: options.body.email_ids.map(email_id => ({ email_id, status: "SENT" })) };
  });
  const pending = operation.approveAndProcess(bulkRows(25, "PENDING_APPROVAL"), Array.from({ length: 12 }, (_, index) => index + 2));
  assert.deepEqual(calls.map(row => row[0]), ["/email-queue/approve"]);
  confirmApproval({ email_ids: Array.from({ length: 12 }, (_, index) => index + 2), approved_count: 12 });
  const result = await pending;
  assert.equal(result.results.length, 12);
  assert.deepEqual(calls.map(row => row[0]), ["/email-queue/approve", "/email-queue/process", "/email-queue/process"]);
  assert.deepEqual(calls.slice(1).flatMap(row => row[1]), Array.from({ length: 12 }, (_, index) => index + 2));
});

test("combined approval failures and unknown responses never start processing", async () => {
  for (const response of [null, { email_ids: [1, 2] }, new Error("offline")]) {
    const calls = [];
    const operation = new BulkEmailOperation(async path => {
      calls.push(path);
      if (response instanceof Error) throw response;
      return response;
    });
    await assert.rejects(() => operation.approveAndProcess(bulkRows(3, "PENDING_APPROVAL"), [1, 3]));
    assert.deepEqual(calls, ["/email-queue/approve"]);
  }
});

test("unconfirmed bulk results require refresh before either approval or processing can be retried", async () => {
  let calls = 0;
  const operation = new BulkEmailOperation(async () => { calls += 1; throw new Error("offline"); });
  await assert.rejects(() => operation.approve(bulkRows(1, "PENDING_APPROVAL"), [1]));
  assert.equal(operation.unknown, true);
  await assert.rejects(() => operation.approve(bulkRows(1, "PENDING_APPROVAL"), [1]), /Refresh/);
  await assert.rejects(() => operation.process(bulkRows(1), [1]), /Refresh/);
  assert.equal(calls, 1);
});

test("leaving the email page stops later client chunks without repeating the in-flight chunk", async () => {
  const calls = [];
  let finish;
  const operation = new BulkEmailOperation((path, options) => {
    calls.push(options.body.email_ids);
    return new Promise(resolve => { finish = resolve; });
  });
  const pending = operation.process(bulkRows(20), Array.from({ length: 20 }, (_, index) => index + 1));
  operation.cancel();
  finish({ results: calls[0].map(email_id => ({ email_id, status: "SENT" })) });
  assert.equal((await pending).length, 10);
  assert.equal(calls.length, 1);
});
