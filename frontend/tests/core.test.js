import assert from "node:assert/strict";
import test from "node:test";
import { dateRange, escape, positive, query, ServingOperation, timestamp } from "../src/core.js";

test("untrusted employee and report values are HTML escaped", () => {
  assert.equal(escape('<img src=x onerror="test">&\''), "&lt;img src=x onerror=&quot;test&quot;&gt;&amp;&#39;");
  assert.equal(escape(null), "");
});

test("date filters include the full final local day", () => {
  const range = dateRange("2026-09-09", "2026-09-09");
  const start = new Date(range.start);
  const end = new Date(range.end);
  assert.equal(start.getHours(), 0);
  assert.equal(start.getDate(), 9);
  assert.equal(end.getDate(), 10);
  assert.equal(end.getHours(), 0);
});

test("date filters reject impossible or reversed dates", () => {
  for (const [start, end] of [["2026-02-30", "2026-03-01"], ["2026-09-10", "2026-09-09"], ["invalid", "2026-09-09"]]) {
    assert.throws(() => dateRange(start, end));
  }
});

test("positive identifiers reject invalid values", () => {
  assert.equal(positive("8", "employee"), 8);
  for (const value of [0, -1, "", "abc", 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => positive(value, "employee"));
});

test("query filters omit absent values and safely encode search", () => {
  assert.equal(query({ q: "Asha & Rao", active: false, after_id: null, limit: 30, empty: "" }), "q=Asha+%26+Rao&active=false&limit=30");
});

test("UTC report timestamps tolerate MySQL naive datetime strings", () => {
  assert.notEqual(timestamp("2026-09-09 12:00:00"), "—");
  assert.equal(timestamp("invalid"), "—");
});

test("concurrent camera frames submit only one request", async () => {
  let release;
  let calls = 0;
  const operation = new ServingOperation(() => "request-1");
  const details = { token: "credential", meal_type_id: 1, scanner_code: "COUNTER-1", quantity: 1 };
  const send = async () => { calls += 1; return new Promise(resolve => { release = resolve; }); };
  const first = operation.submit(send, details);
  const competing = await operation.submit(send, details);
  assert.equal(competing, null);
  assert.equal(calls, 1);
  release({ approved: true, code: "APPROVED", request_id: "request-1", serving_id: 1, meal_ids: [1] });
  await first;
  assert.equal(operation.inFlight, false);
});

test("uncertain network outcome retains original identifier and payload", async () => {
  const operation = new ServingOperation(() => "request-1");
  const details = { token: "original-credential", meal_type_id: 1, scanner_code: "COUNTER-1", quantity: 1 };
  let initial;
  await assert.rejects(operation.submit(async body => { initial = body; throw new Error("offline"); }, details));
  let retried;
  await operation.submit(async body => { retried = body; return { approved: true, code: "APPROVED", request_id: body.request_id, serving_id: 1, meal_ids: [1] }; }, { token: "changed-credential", meal_type_id: 2 });
  assert.deepEqual(retried, initial);
  assert.equal(retried.request_id, "request-1");
});

test("completed request returns original result without resubmission", async () => {
  const operation = new ServingOperation(() => "request-1");
  let calls = 0;
  const send = async body => { calls += 1; return { approved: true, code: "APPROVED", request_id: body.request_id, serving_id: 5, meal_ids: [7] }; };
  const first = await operation.submit(send, { token: "credential" });
  const duplicate = await operation.submit(send, { token: "credential" });
  assert.equal(calls, 1);
  assert.deepEqual(duplicate, { ...first, duplicate: true });
});

test("new intentional serving obtains another identifier", async () => {
  let sequence = 0;
  const operation = new ServingOperation(() => `request-${++sequence}`);
  const first = operation.bind({ token: "reusable-credential" });
  operation.reset();
  const second = operation.bind({ token: "reusable-credential" });
  assert.notEqual(first.request_id, second.request_id);
  assert.equal(first.token, second.token);
});

test("master scan retains the administrator authorization request", () => {
  const operation = new ServingOperation(() => "unused-identifier");
  const request = operation.bind({ token: "master-credential", request_id: "authorized-request", authorization_id: 17, quantity: 4 });
  assert.equal(request.request_id, "authorized-request");
  assert.equal(request.quantity, 4);
  assert.equal(request.authorization_id, 17);
});
