import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { BulkEmployeeImport, employeeFileKind, employeePayloadFingerprint, parseEmployeeCsv as parseCsv, parseEmployeeCsvImport as parseCsvImport, parseEmployeeImport as parseImport, parseEmployeeRows as parseRows, sampleEmployeeCsv as sampleCsv } from "../src/bulk_employees.js";
import { readFile } from "node:fs/promises";
import { employeeImportForm, ignoredColumnsNote } from "../src/employee_import.js";

const mealTypes = [{ id: 2, code: "BREAKFAST", name: "Breakfast", is_active: true }, { id: 3, code: "LUNCH", name: "Lunch", is_active: true }, { id: 4, code: "HIGH-TEA", name: "High Tea", is_active: true }, { id: 5, code: "DINNER", name: "Dinner", is_active: true }, { id: 1, code: "SCANNER-MEAL", name: "Scanner meal", is_active: false }];
const withMeals = (options = {}) => ({ mealTypes, ...options });
const parseEmployeeCsv = (text, rows, options) => parseCsv(text, rows, withMeals(options));
const parseEmployeeCsvImport = (text, rows, options) => parseCsvImport(text, rows, withMeals(options));
const parseEmployeeImport = (table, rows, options) => parseImport(table, rows, withMeals(options));
const parseEmployeeRows = (table, rows, options) => parseRows(table, rows, withMeals(options));
const sampleEmployeeCsv = rows => sampleCsv(rows, mealTypes);
const headers = "employee_code,full_name,email,company_name,phone,meal_types,department";
const departments = [{ id: 4, name: "Facilities", is_active: true }, { id: 6, name: "Inactive", is_active: false }];
const csv = `${headers}\nEMP-01,Fictional Person,Person@example.test,Example Company,+1 202 555 0101,BREAKFAST,Facilities`;
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
  const rows = parseEmployeeCsv('\uFEFFemail,department,phone,full_name,company_name,employee_code,meal_types\r\nPerson@example.test,Facilities,+1 202 555 0101,"Person, Fictional",Example Company,EMP-01, breakfast ; High-Tea \r\n', departments);
  assert.deepEqual(rows, [{ employee_code: "EMP-01", full_name: "Person, Fictional", email: "person@example.test", company_name: "Example Company", phone: "+1 202 555 0101", department: "Facilities", meal_type_ids: [2, 4] }]);
  const quoted = parseEmployeeCsv(`${headers}\nEMP-01,"Fictional ""Name""",person@example.test,Example Company,+1 202 555 0101,BREAKFAST,`, departments);
  assert.equal(quoted[0].full_name, 'Fictional "Name"');
});

test("invalid CSV structure and invalid employee fields fail before requests", () => {
  for (const text of ["", headers, `wrong,headers\na,b`, `${headers},extra\na,b,c,d,e,4,extra`, `${headers}\na,b,c,d,e,4,extra`, `${headers}\na,"unfinished,c,d,e,4`, `${headers}\na,"name"bad,c,d,e,4`, `${headers}\n,Name,person@example.test,Example Company,+1 202 555 0101,BREAKFAST,4`, `${headers}\nA,Name,invalid-email,Example Company,+1 202 555 0101,BREAKFAST,4`, `${headers}\nA,Name,person@example.test,,+1 202 555 0101,BREAKFAST,4`, `${headers}\nA,Name,person@example.test,Example Company,invalid,BREAKFAST,4`, `${headers}\nA,Name,person@example.test,Example Company,+1 202 555 0101,BREAKFAST,Inactive`, `${headers}\nA,Name,person@example.test,Example Company,+1 202 555 0101,BREAKFAST,Nowhere`, `${headers}\nA,Name,person@example.test,Example Company,+1 202 555 0101,BREAKFAST,4`, `${csv}\nemp-01,Other,other@example.test,Example Company,+1 202 555 0102,BREAKFAST,4`]) assert.throws(() => parseEmployeeCsv(text, departments));
});

test("CSV enforces 100 rows and file size limits", () => {
  const row = index => `E${index},Name ${index},p${index}@example.test,Example Company,+1 202 555 ${String(index).padStart(4, "0")},BREAKFAST,`;
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
  assert.doesNotMatch(html, /department_id|Department ID|department ID/);
  assert.match(html, /Department names<\/summary><p class="field-help">[^<]*Facilities/);
  assert.doesNotMatch(html, /Department names<\/summary><p class="field-help">[^<]*Inactive/);
  assert.match(html, /id="employee-sample">Download sample CSV/);
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
  await assert.rejects(() => operation.prepare([{ ...employees[0], employee_code: "DIFFERENT" }]), /original file/);
  await operation.submit();
  assert.deepEqual(calls[0], calls[1]);
});

test("reload recovery keeps only opaque values and requires original CSV before retry", async () => {
  const { operation, storage, options } = setup(async () => { throw new Error("offline"); });
  await operation.prepare(employees);
  await assert.rejects(() => operation.submit());
  const saved = storage.getItem(operation.storageKey);
  assert.doesNotMatch(saved, /Fictional|EMP-01|example|department|Facilities|email|full_name/);
  assert.deepEqual(Object.keys(JSON.parse(saved)).sort(), ["employee_count", "payload_fingerprint", "request_id"]);
  const calls = [];
  const restored = new BulkEmployeeImport(1, async (...args) => { calls.push(args); return { ...result, replayed: true }; }, options);
  await assert.rejects(() => restored.submit(), /original employee CSV/);
  await assert.rejects(() => restored.prepare([{ ...employees[0], employee_code: "OTHER" }]), /original file/);
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
  assert.throws(() => parseEmployeeCsv(csv, inactive), /department "Facilities" is inactive/);
  const restored = new BulkEmployeeImport(1, async () => ({ ...result, replayed: true }), options);
  await restored.prepare(parseEmployeeCsv(csv, inactive, { recovering: true }));
  assert.equal((await restored.submit()).replayed, true);
});

test("Excel rows from the server go through the same checks as CSV", () => {
  const table = [headers.split(","), ["EMP-01", "Fictional Person", "Person@example.test", "Example Company", "9876543210", "BREAKFAST", "facilities"]];
  assert.deepEqual(parseEmployeeRows(table, departments), [{ employee_code: "EMP-01", full_name: "Fictional Person", email: "person@example.test", company_name: "Example Company", phone: "9876543210", department: "Facilities", meal_type_ids: [2] }]);
  assert.deepEqual(parseEmployeeRows(table, departments), parseEmployeeCsv(table.map(row => row.join(",")).join("\n"), departments));
  assert.throws(() => parseEmployeeRows([headers.split(","), ["EMP-01", "Fictional Person", "p@example.test", "Example Company", "9876543210", "BREAKFAST", "Inactive"]], departments), /Row 2: department "Inactive" is inactive/);
  assert.throws(() => parseEmployeeRows([["code", "name"], ["a", "b"]], departments), /Use these headers/);
  for (const bad of [null, [["a", 1]], ["row"]]) assert.throws(() => parseEmployeeRows(bad, departments), /could not be read/);
});

test("the import accepts .csv and .xlsx only and reads Excel through the server", async () => {
  assert.equal(employeeFileKind({ name: "Staff.CSV" }), "csv");
  assert.equal(employeeFileKind({ name: "staff.xlsx" }), "xlsx");
  for (const name of ["staff.xls", "staff.xlsm", "staff.ods", "staff.txt", ""]) assert.throws(() => employeeFileKind({ name }), /\.csv or \.xlsx/);
  const form = employeeImportForm(departments);
  assert.match(form, /Upload CSV or Excel<input name="csv_file" type="file" accept="\.csv,\.xlsx,text\/csv,application\/vnd\.openxmlformats-officedocument\.spreadsheetml\.sheet">/);
  assert.match(form, /an Excel file is sent to the server only to read it for the preview/);
  const source = await readFile(new URL("../src/employee_import.js", import.meta.url), "utf8");
  assert.match(source, /api\(`\/employees\/import-file\?\$\{query\(\{ filename: selectedFile\.name \}\)\}`, \{ method: "POST", body: new Blob\(\[selectedFile\], \{ type: EMPLOYEE_XLSX \}\) \}\)/);
  assert.match(source, /parseEmployeeImport\(result\.rows, context\.catalog\.departments, options\)/);
});

const clientDepartments = [{ id: 3, name: "Exhibition Centre", is_active: true }, { id: 9, name: "AI Studio", is_active: false }, { id: 11, name: "Quality Office", is_active: true }];
const base = "employee_code,full_name,email,company_name,phone,meal_types";

test("a department_id column is ignored and named in the preview note", () => {
  const parsed = parseEmployeeCsvImport(`${base},department_id\nE-1,Asha Rao,asha@mcciapune.in,MCCIA,+91 98765 43210,BREAKFAST,7\nE-2,Ravi,ravi@mcciapune.in,MCCIA,+91 98765 43211,BREAKFAST,16\n`, clientDepartments);
  assert.deepEqual(parsed.ignored, ["department_id"]);
  assert.deepEqual(parsed.employees.map(row => row.department), [null, null]);
  assert.ok(parsed.employees.every(row => !("department_id" in row)));
  assert.equal(ignoredColumnsNote(parsed.ignored), "Ignored column: department_id. Departments come from the optional department column.");
  assert.equal(ignoredColumnsNote([]), "");
  const both = parseEmployeeCsvImport(`${base},department_id,department\nE-1,Asha,asha@mcciapune.in,MCCIA,+91 98765 43210,BREAKFAST,4,Quality Office\n`, clientDepartments);
  assert.equal(both.employees[0].department, "Quality Office");
});

test("department names match ignoring case, accents and extra spaces, including built-in facilities", () => {
  const names = ["  exhibition   CENTRE ", "quality office", "Rubber & Polymer Testing Lab", "AUDITORIUM", "", "   ", "Éxhibition Centre"];
  const table = [[...base.split(","), "department"], ...names.map((department, index) => [`E-${index}`, "Name", `p${index}@mcciapune.in`, "MCCIA", `+91 98765 4321${index}`, "BREAKFAST", department])];
  assert.deepEqual(parseEmployeeImport(table, clientDepartments).employees.map(row => row.department),
    ["Exhibition Centre", "Quality Office", "Rubber & Polymer Testing Lab", "Auditorium", null, null, "Exhibition Centre"]);
});

test("unknown and deactivated department names are row errors listing the valid names", () => {
  const file = name => `${base},department\nE-1,Asha,asha@mcciapune.in,MCCIA,+91 98765 43210,BREAKFAST,${name}\n`;
  assert.throws(() => parseEmployeeCsv(file("AI Studio"), clientDepartments), /Row 2: department "AI Studio" is inactive\. Use one of: .*Exhibition Centre.*Quality Office, or leave it blank\./);
  assert.throws(() => parseEmployeeCsv(file("Department 7"), clientDepartments), /Row 2: department "Department 7" is not a known department\. Use one of: Prototype Production Facility, /);
  assert.throws(() => parseEmployeeCsv(file("Department 7"), clientDepartments), error => !/AI Studio/.test(error.message));
  assert.doesNotThrow(() => parseEmployeeCsv(file(""), clientDepartments));
});

test("the sample file uses the new headers and parses cleanly", () => {
  const sample = sampleEmployeeCsv(clientDepartments);
  assert.match(sample, /^employee_code,full_name,email,company_name,phone,department,meal_types\r\n/);
  assert.equal(sample.match(/,BREAKFAST;HIGH-TEA\r\n/g).length, 2);
  assert.deepEqual(parseEmployeeCsvImport(sample, clientDepartments).employees.map(row => row.meal_type_ids), [[2, 4], [2, 4]]);
  assert.doesNotMatch(sample, /department_id/);
  const parsed = parseEmployeeCsvImport(sample, clientDepartments);
  assert.deepEqual(parsed.employees.map(row => row.department), ["Prototype Production Facility", null]);
  assert.deepEqual(parsed.ignored, []);
});

test("meal_types is required: a missing column or blank cell lists the valid codes", () => {
  const noColumn = "employee_code,full_name,email,company_name,phone,department\nE-1,Asha,asha@mcciapune.in,MCCIA,+91 98765 43210,\n";
  assert.throws(() => parseEmployeeCsv(noColumn, departments), /^Error: Add a meal_types column\. .*Valid codes: BREAKFAST, LUNCH, HIGH-TEA, DINNER\.$/);
  const blank = `${headers}\nE-1,Asha,asha@mcciapune.in,MCCIA,+91 98765 43210,,\nE-2,Ravi,ravi@mcciapune.in,MCCIA,+91 98765 43211, ; ,\n`;
  assert.throws(() => parseEmployeeCsv(blank, departments), /^Error: Row 2: meal_types is required\. Use codes separated by ;\. Valid codes: BREAKFAST, LUNCH, HIGH-TEA, DINNER\.$/);
  assert.throws(() => parseEmployeeCsv(`${headers}\nE-2,Ravi,ravi@mcciapune.in,MCCIA,+91 98765 43211, ; ,\n`, departments), /Row 2: meal_types is required/);
  assert.throws(() => parseEmployeeCsv(`${headers}\nE-1,Asha,asha@mcciapune.in,MCCIA,+91 98765 43210,SCANNER-MEAL,\n`, departments), /Row 2: meal_types contains an unknown or inactive meal type code: SCANNER-MEAL\. Valid codes: BREAKFAST, LUNCH, HIGH-TEA, DINNER\./);
  const excel = [headers.split(","), ["E-1", "Asha", "asha@mcciapune.in", "MCCIA", "9876543210", "", ""]];
  assert.throws(() => parseEmployeeRows(excel, departments), /Row 2: meal_types is required/);
  assert.throws(() => parseEmployeeRows([excel[0].filter(name => name !== "meal_types"), ["E-1", "Asha", "asha@mcciapune.in", "MCCIA", "9876543210", ""]], departments), /Add a meal_types column/);
  const form = employeeImportForm(departments, false, mealTypes);
  assert.match(form, /meal_types<\/span> is required on every row/);
  assert.match(form, /Valid codes: <span class="mono">BREAKFAST, LUNCH, HIGH-TEA, DINNER<\/span>/);
  assert.doesNotMatch(form, /blank means all active meal types/);
});
