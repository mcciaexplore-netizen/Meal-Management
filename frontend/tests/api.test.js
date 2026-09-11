import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { allPages, api, clearCredentials, onUnauthorized } from "../src/api.js";
import { qrCard, qrStatus } from "../src/ui.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  clearCredentials();
  onUnauthorized(() => {});
});

test("read requests use private same-origin responses", async () => {
  let captured;
  globalThis.fetch = async (path, config) => { captured = { path, config }; return Response.json({ items: [] }); };
  await api("/employees");
  assert.equal(captured.path, "/api/employees");
  assert.equal(captured.config.credentials, "same-origin");
  assert.equal(captured.config.cache, "no-store");
  assert.equal(captured.config.headers["X-CSRF-Token"], undefined);
});

test("mutations acquire a CSRF token and submit a JSON request", async () => {
  const calls = [];
  globalThis.fetch = async (path, config) => {
    calls.push({ path, config });
    return Response.json(path === "/api/auth/csrf" ? { csrf_token: "unit-test-csrf" } : { employee_id: 1 });
  };
  await api("/employees", { method: "POST", body: { full_name: "Test Employee" } });
  assert.equal(calls.length, 2);
  assert.equal(calls[1].config.headers["X-CSRF-Token"], "unit-test-csrf");
  assert.equal(calls[1].config.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[1].config.body), { full_name: "Test Employee" });
});

test("private photo uploads preserve image type and CSRF protection", async () => {
  let upload;
  globalThis.fetch = async (path, config) => {
    if (path.endsWith("/csrf")) return Response.json({ csrf_token: "unit-test-csrf" });
    upload = config;
    return Response.json({ selfie_object_key: "private/reference" });
  };
  const photo = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
  await api("/employees/1/photo", { method: "POST", body: photo });
  assert.equal(upload.body, photo);
  assert.equal(upload.headers["Content-Type"], "image/png");
  assert.equal(upload.headers["X-CSRF-Token"], "unit-test-csrf");
});

test("expired sessions invoke the unauthenticated handler", async () => {
  let invoked = 0;
  onUnauthorized(() => { invoked += 1; });
  globalThis.fetch = async () => Response.json({ error: { code: "AUTHENTICATION_REQUIRED", message: "Please sign in." } }, { status: 401 });
  await assert.rejects(api("/employees"), error => error.status === 401 && error.code === "AUTHENTICATION_REQUIRED");
  assert.equal(invoked, 1);
});

test("malformed server errors do not expose response bodies", async () => {
  globalThis.fetch = async () => new Response("internal database exception", { status: 500 });
  await assert.rejects(api("/employees"), error => !error.message.includes("database") && error.status === 500);
});

test("network errors describe uncertain scan results", async () => {
  globalThis.fetch = async () => { throw new Error("internal transport detail"); };
  await assert.rejects(api("/employees"), error => error.message.includes("same serving") && !error.message.includes("internal transport"));
});

test("approval selection loads subsequent pages", async () => {
  const paths = [];
  globalThis.fetch = async path => {
    paths.push(path);
    return Response.json(paths.length === 1 ? { items: [{ id: 1 }], next_cursor: 1 } : { items: [{ id: 2 }], next_cursor: null });
  };
  assert.deepEqual(await allPages("/visitor-authorizations?status=pending"), [{ id: 1 }, { id: 2 }]);
  assert.match(paths[1], /status=pending&limit=100&after_id=1$/);
});

test("pagination detects a repeated server cursor", async () => {
  globalThis.fetch = async () => Response.json({ items: [], next_cursor: 7 });
  await assert.rejects(allPages("/master-qrs"), /list could not be loaded/);
});

test("inactive and revoked QR views do not render a credential image", () => {
  assert.match(qrStatus({ credential_status: "EMPLOYEE_INACTIVE" }), /Employee inactive/);
  const markup = qrCard({ svg: null, revoked_at: "2026-09-09T12:00:00Z" });
  assert.match(markup, /Revoked/);
  assert.doesNotMatch(markup, /<img/);
});

test("exhausted master QR cards show expiry and hide credential images", () => {
  for (const details of [{ exhausted_at: "2026-09-11T12:00:00Z" }, { meals_remaining: 0 }, { credential_status: "QR_EXPIRED" }]) {
    const qr = { kind: "MASTER", meal_limit: 5, svg: "<svg></svg>", ...details };
    assert.match(qrStatus(qr), /Expired/);
    assert.match(qrCard(qr), /This credential has expired/);
    assert.doesNotMatch(qrCard(qr), /<img|>Active</);
  }
});

test("legacy master QR with no allowance is distinguished from an exhausted group QR", () => {
  const qr = { kind: "MASTER", meal_limit: null, meals_remaining: null, svg: "<svg></svg>" };
  assert.match(qrStatus(qr), /Create visitor QR/);
  assert.doesNotMatch(qrStatus(qr), /Expired|Active/);
  assert.match(qrCard(qr), /no meal allowance/);
  assert.doesNotMatch(qrCard(qr), /<img/);
});

test("employee QRs remain active when master allowance fields are null", () => {
  const qr = { kind: "EMPLOYEE", meal_limit: null, meals_remaining: null, svg: "<svg></svg>" };
  assert.match(qrStatus(qr), /Active/);
  assert.match(qrCard(qr), /<img/);
});

test("master QR with meals left remains active", () => {
  const qr = { kind: "MASTER", meal_limit: 5, meals_remaining: 2, svg: "<svg></svg>" };
  assert.match(qrStatus(qr), /Active/);
  assert.match(qrCard(qr), /<img/);
});
