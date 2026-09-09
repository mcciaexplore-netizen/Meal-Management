import assert from "node:assert/strict";
import test from "node:test";
import { ScannerTransport } from "../src/scanner_api.js";

const scope = "a".repeat(64);
const csrf = "b".repeat(64);
const session = () => Response.json({ scope, csrf_token: csrf });
const request = { request_id: "10000000-0000-4000-8000-000000000001", token: "test-token" };
const approved = { request_id: request.request_id, approved: true, code: "APPROVED", serving_id: 1, meal_ids: [1] };

test("public scanner boot establishes only its own private browser session", async () => {
  const calls = [];
  const transport = new ScannerTransport(async (path, options) => { calls.push({ path, options }); return session(); });
  assert.deepEqual(await transport.connect(), { scope });
  assert.equal(transport.scope, scope);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/scanner/session");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
});

test("public scanning uses scanner CSRF and sends the projected original payload", async () => {
  const calls = [];
  const transport = new ScannerTransport(async (path, options) => {
    calls.push({ path, options });
    return path.endsWith("/session") ? session() : Response.json(approved);
  });
  const result = await transport.request("/read", { method: "POST", body: request });
  assert.equal(result.approved, true);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].path, "/api/scanner/read");
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[1].options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[1].options.body), request);
  assert.equal(calls[1].options.credentials, "same-origin");
});

test("pending result recovery is scoped to the same browser without staff endpoints", async () => {
  let resultCall;
  const transport = new ScannerTransport(async (path, options) => {
    if (path.endsWith("/session")) return session();
    resultCall = { path, options };
    return Response.json(approved);
  });
  await transport.connect();
  await transport.request(`/requests/${request.request_id}/result`, {}, scope);
  assert.equal(resultCall.path, `/api/scanner/requests/${request.request_id}/result`);
  assert.equal(resultCall.options.method, "GET");
  assert.equal(resultCall.options.headers["X-CSRF-Token"], undefined);
});

test("expired scanner cookies refresh silently with the same original serving", async () => {
  let sessions = 0;
  const bodies = [];
  const transport = new ScannerTransport(async (path, options) => {
    if (path.endsWith("/session")) { sessions += 1; return session(); }
    bodies.push(options.body);
    return bodies.length === 1 ? Response.json({ error: { code: "SCANNER_BROWSER_REQUIRED" } }, { status: 401 }) : Response.json(approved);
  });
  await transport.connect();
  const result = await transport.request("/read", { method: "POST", body: request }, scope);
  assert.equal(result.approved, true);
  assert.equal(sessions, 2);
  assert.deepEqual(bodies, [JSON.stringify(request), JSON.stringify(request)]);
});

test("scanner CSRF renewal retains browser scope and uses the renewed token", async () => {
  let sessions = 0;
  const headers = [];
  const renewed = "c".repeat(64);
  const transport = new ScannerTransport(async (path, options) => {
    if (path.endsWith("/session")) return Response.json({ scope, csrf_token: ++sessions === 1 ? csrf : renewed });
    headers.push(options.headers["X-CSRF-Token"]);
    return headers.length === 1 ? Response.json({ error: { code: "CSRF_REJECTED" } }, { status: 403 }) : Response.json(approved);
  });
  await transport.request("/read", { method: "POST", body: request });
  assert.deepEqual(headers, [csrf, renewed]);
});

test("a changed browser scope cannot resubmit a pending serving", async () => {
  let sessions = 0;
  let submits = 0;
  const transport = new ScannerTransport(async path => {
    if (path.endsWith("/session")) return Response.json({ scope: ++sessions === 1 ? scope : "c".repeat(64), csrf_token: csrf });
    submits += 1;
    return Response.json({ error: { code: "SCANNER_BROWSER_REQUIRED" } }, { status: 401 });
  });
  await transport.connect();
  await assert.rejects(transport.request("/read", { method: "POST", body: request }, scope), error => error.code === "SCANNER_SCOPE_CHANGED");
  await assert.rejects(transport.request("/read", { method: "POST", body: request }, scope), error => error.code === "SCANNER_SCOPE_CHANGED");
  assert.equal(submits, 1);
});

test("network interruption stays uncertain and refreshes the browser session on retry", async () => {
  let sessions = 0;
  let submits = 0;
  const transport = new ScannerTransport(async path => {
    if (path.endsWith("/session")) { sessions += 1; return session(); }
    if (++submits === 1) throw new Error("Internal connection detail");
    return Response.json(approved);
  });
  await transport.connect();
  await assert.rejects(transport.request("/read", { method: "POST", body: request }, scope), error => error.message.includes("same serving") && !error.message.includes("Internal"));
  assert.equal(submits, 1);
  const result = await transport.request("/read", { method: "POST", body: request }, scope);
  assert.equal(sessions, 2);
  assert.equal(result.approved, true);
});

test("origin rejection does not refresh or resubmit", async () => {
  let calls = 0;
  const transport = new ScannerTransport(async path => {
    calls += 1;
    return path.endsWith("/session") ? session() : Response.json({ error: { code: "ORIGIN_REJECTED" } }, { status: 403 });
  });
  await assert.rejects(transport.request("/read", { method: "POST", body: request }), error => error.code === "ORIGIN_REJECTED");
  assert.equal(calls, 2);
});

test("unavailable and invalid session responses never create a scanner identity", async () => {
  for (const response of [new Response("Database internal details", { status: 503 }), Response.json({ scope, csrf_token: "invalid" })]) {
    const transport = new ScannerTransport(async () => response);
    await assert.rejects(transport.connect(), error => !error.message.includes("Database") && !/sign in|login/i.test(error.message));
    assert.equal(transport.scope, null);
  }
});
