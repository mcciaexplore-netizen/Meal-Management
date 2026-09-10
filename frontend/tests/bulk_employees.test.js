import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { BulkEmployeeImport, employeePayloadFingerprint, parseEmployeeCsv } from "../src/bulk_employees.js";
import { employeeImportForm } from "../src/employee_import.js";

const headers = "employee_code,full_name,email,department_id";
const departments = [{ id: 4, name: "Facilities", is_active: true }, { id: 6, name: "Inactive", is_active: false }];
const csv = `${headers}\nEMP-01,Fictional Person,Person@example.test,4`;
const employees = parseEmployeeCsv(csv, departments);
const requestId = "22222222-2222-4222-8222-222222222222";
const result = { batch_id: 8, employees: [{ employee_id: 2, qr_id: 3, email_id: 4 }], replayed: false };
const fingerprint = rows => employeePayloadFingerprint(rows, webcrypto);
const memoryStorage = () => {
  const values = new Map();
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
};
const setup = request => {
  const storage = memoryStorage();
  const options = { storage, createIdentifier: () => requestId, fingerprint };
  return { storage, options, operation: new BulkEmployeeImport(1, request, options) };
};

test("CSV parsing supports reordered headers, BOM, CRLF and quoted commas", () => {
  const rows = parseEmployeeCsv('\uFEFFemail,department_id,full_name,employee_code\r\nPerson@example.test,4,"Person, Fictional",EMP-01\r\n', departments);
  assert.deepEqual(rows, [{ employee_code: "EMP-01", full_name: "Person, Fictional", email: "person@example.test", department_id: 4 }]);
  const quoted = parseEmployeeCsv(`${headers}\nEMP-01,"Fictional ""Name""",person@example.test,4`, departments);
  assert.equal(quoted[0].full_name, 'Fictional "Name"');
});

test("invalid CSV structure and invalid employee fields fail before requests", () => {
  for (const text of ["", headers, `wrong,headers\na,b`, `${headers},extra\na,b,c,4,d`, `${headers}\na,b,c,4,extra`, `${headers}\na,"unfinished,c,4`, `${headers}\na,"name"bad,c,4`, `${headers}\n,Name,person@example.test,4`, `${headers}\nA,Name,invalid-email,4`, `${headers}\nA,Name,person@example.test,6`, `${headers}\nA,Name,person@example.test,99`, `${headers}\nA,Name,person@example.test,1e2`, `${csv}\nemp-01,Other,other@example.test,4`]) assert.throws(() => parseEmployeeCsv(text, departments));
});

test("CSV enforces 100 rows and file size limits", () => {
  const row = index => `E${index},Name ${index},p${index}@example.test,4`;
  assert.equal(parseEmployeeCsv(`${headers}\n${Array.from({ length: 100 }, (_, index) => row(index)).join("\n")}`, departments).length, 100);
  assert.throws(() => parseEmployeeCsv(`${headers}\n${Array.from({ length: 101 }, (_, index) => row(index)).join("\n")}`, departments), /100/);
  assert.throws(() => parseEmployeeCsv("x".repeat(65537), departments), /64 KB/);
});

test("bulk preview prepares data without any API or email action", async () => {
  const calls = [];
  const { operation } = setup(async (...args) => { calls.push(args); return result; });
  await operation.prepare(employees);
  assert.equal(calls.length, 0);
  assert.equal(operation.submitted, false);
  const html = employeeImportForm(departments);
  assert.match(html, /this import does not send email/);
  assert.match(html, /department_id/);
  assert.match(html, />4</);
  assert.doesNotMatch(html, /type="submit"[^>]*>Send/);
});

test("successful bulk submission creates records only and clears its opaque recovery marker", async () => {
  const calls = [];
  const { operation, storage } = setup(async (...args) => { calls.push(args); return result; });
  await operation.prepare(employees);
  assert.deepEqual(await operation.submit(), result);
  assert.deepEqual(calls, [["/employees/bulk", { method: "POST", body: { request_id: requestId, employees } }]]);
  assert.equal(storage.values.size, 0);
  assert.deepEqual(await operation.submit(), result);
  assert.equal(calls.length, 1);
});

test("concurrent submit clicks cannot duplicate the import request", async () => {
  let resolve;
  let calls = 0;
  const { operation } = setup(() => { calls += 1; return new Promise(done => { resolve = done; }); });
  await operation.prepare(employees);
  const first = operation.submit();
  assert.equal(await operation.submit(), null);
  await assert.rejects(() => operation.prepare(employees), /Wait for this import/);
  resolve(result);
  await first;
  assert.equal(calls, 1);
});

test("unknown bulk submission freezes UUID and payload across a retry", async () => {
  const calls = [];
  const { operation } = setup(async (...args) => { calls.push(args); if (calls.length === 1) throw new Error("offline"); return { ...result, replayed: true }; });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit(), /not confirmed/);
  await assert.rejects(() => operation.prepare([{ ...employees[0], employee_code: "DIFFERENT" }]), /original CSV/);
  await operation.submit();
  assert.deepEqual(calls[0], calls[1]);
});

test("reload recovery keeps only opaque values and requires original CSV before retry", async () => {
  const { operation, storage, options } = setup(async () => { throw new Error("offline"); });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit());
  const saved = storage.getItem(operation.storageKey);
  assert.doesNotMatch(saved, /Fictional|EMP-01|example|department_id|email|full_name/);
  assert.deepEqual(Object.keys(JSON.parse(saved)).sort(), ["employee_count", "payload_fingerprint", "request_id"]);
  const calls = [];
  const restored = new BulkEmployeeImport(1, async (...args) => { calls.push(args); return { ...result, replayed: true }; }, options);
  await assert.rejects(() => restored.submit(), /original employee CSV/);
  await assert.rejects(() => restored.prepare([{ ...employees[0], employee_code: "OTHER" }]), /original CSV/);
  await restored.prepare(employees);
  await restored.submit();
  assert.equal(calls[0][1].body.request_id, requestId);
  assert.equal(storage.values.size, 0);
});

test("caller scope and corrupt markers cannot expose or discard another pending import", async () => {
  const { operation, storage, options } = setup(async () => { throw new Error("offline"); });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit());
  const other = new BulkEmployeeImport(2, async () => result, options);
  assert.equal(other.marker, null);
  storage.setItem("meal_employee_import_v1:3", "not-json");
  const damaged = new BulkEmployeeImport(3, async () => result, options);
  await assert.rejects(() => damaged.prepare(employees), /could not be recovered/);
  assert.ok(storage.getItem(operation.storageKey));
});

test("unavailable recovery storage prevents submitting any import", async () => {
  let calls = 0;
  const operation = new BulkEmployeeImport(1, async () => { calls += 1; return result; }, { storage: null, createIdentifier: () => requestId, fingerprint });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit(), /preserve import retries/);
  assert.equal(calls, 0);
});

test("malformed results never unlock a pending import", async () => {
  for (const response of [null, { ...result, employees: [] }, { ...result, batch_id: null }, { ...result, replayed: undefined }]) {
    const { operation, storage } = setup(async () => response);
    await operation.prepare(employees);
    await assert.rejects(() => operation.submit(), /not confirmed/);
    assert.equal(operation.result, null);
    assert.ok(storage.getItem(operation.storageKey));
  }
});

test("fresh input validation can be corrected but a retry validation response cannot discard an unknown original", async () => {
  const error = Object.assign(new Error("invalid input"), { status: 422 });
  const fresh = setup(async () => { throw error; });
  await fresh.operation.prepare(employees);
  await assert.rejects(() => fresh.operation.submit(), /not accepted/);
  assert.equal(fresh.operation.submitted, false);
  assert.equal(fresh.storage.values.size, 0);
  let attempts = 0;
  const uncertain = setup(async () => { if (attempts++ === 0) throw new Error("offline"); throw error; });
  await uncertain.operation.prepare(employees);
  await assert.rejects(() => uncertain.operation.submit());
  await assert.rejects(() => uncertain.operation.submit(), /not confirmed/);
  assert.equal(uncertain.operation.submitted, true);
  assert.equal(uncertain.storage.values.size, 1);
});

test("fresh confirmed business rejection allows correction while later rejection preserves the original import", async () => {
  for (const [status, code] of [[409, "EMPLOYEE_CODE_EXISTS"], [400, "DEPARTMENT_UNAVAILABLE"], [400, "INVALID_EMAIL"]]) {
    const failure = Object.assign(new Error("safe input failure"), { status, code });
    const fresh = setup(async () => { throw failure; });
    await fresh.operation.prepare(employees);
    await assert.rejects(() => fresh.operation.submit(), /not accepted/);
    assert.equal(fresh.operation.submitted, false);
    assert.equal(fresh.storage.values.size, 0);
    let attempts = 0;
    const pending = setup(async () => { if (attempts++ === 0) throw new Error("offline"); throw failure; });
    await pending.operation.prepare(employees);
    await assert.rejects(() => pending.operation.submit());
    await assert.rejects(() => pending.operation.submit(), /not confirmed/);
    assert.equal(pending.operation.submitted, true);
  }
});

test("idempotency conflicts and unavailable services keep the import locked", async () => {
  for (const [status, code] of [[409, "IDEMPOTENCY_KEY_REUSED"], [503, "SERVICE_UNAVAILABLE"], [503, "TRANSACTION_RETRY_REQUIRED"]]) {
    const { operation, storage } = setup(async () => { throw Object.assign(new Error("unknown outcome"), { status, code }); });
    await operation.prepare(employees);
    await assert.rejects(() => operation.submit(), /not confirmed/);
    assert.equal(operation.submitted, true);
    assert.equal(storage.values.size, 1);
  }
});

test("recovery can validate the original fingerprint even after its department is deactivated", async () => {
  const { operation, options } = setup(async () => { throw new Error("offline"); });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit());
  const inactive = [{ id: 4, name: "Facilities", is_active: false }];
  assert.throws(() => parseEmployeeCsv(csv, inactive), /active department/);
  const restored = new BulkEmployeeImport(1, async () => ({ ...result, replayed: true }), options);
  await restored.prepare(parseEmployeeCsv(csv, inactive, { recovering: true }));
  assert.equal((await restored.submit()).replayed, true);
});
