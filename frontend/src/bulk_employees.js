import { randomIdentifier } from "./core.js";

export const employeeCsvHeaders = ["employee_code", "full_name", "email", "department_id"];
const maximumBytes = 65536;
const requestPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const imports = new Map();
const rejectedImports = new Map([
  ["EMPLOYEE_CODE_EXISTS", "An employee code already exists. Correct that code or remove the existing employee from the CSV."],
  ["DEPARTMENT_UNAVAILABLE", "A department is unavailable. Choose an active department ID."],
  ["INVALID_EMAIL", "Correct the email addresses in the CSV."],
  ["INVALID_EMPLOYEE_CODE", "Correct the employee codes in the CSV."],
  ["INVALID_FULL_NAME", "Correct the employee names in the CSV."],
  ["INVALID_DEPARTMENT_ID", "Correct the department IDs in the CSV."],
  ["INVALID_EMPLOYEE_BATCH", "Correct the employee batch before importing."],
  ["INVALID_REQUEST_ID", "Preview the CSV again to create a valid import request."],
]);

function csvRows(text) {
  if (typeof text !== "string" || new TextEncoder().encode(text).length > maximumBytes) throw new Error("Choose a CSV smaller than 64 KB with at most 100 employees.");
  text = text.replace(/^\uFEFF/, "");
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let endedQuote = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') { field += '"'; index += 1; }
      else if (character === '"') { quoted = false; endedQuote = true; }
      else field += character;
      continue;
    }
    if (character === '"' && field === "" && !endedQuote) { quoted = true; continue; }
    if (character === "," || character === "\n" || character === "\r") {
      row.push(field.trim());
      field = "";
      endedQuote = false;
      if (character !== ",") {
        if (character === "\r" && text[index + 1] === "\n") index += 1;
        if (row.some(value => value !== "")) rows.push(row);
        row = [];
      }
      continue;
    }
    if (character === '"' || endedQuote && !/\s/.test(character)) throw new Error("The CSV contains an invalid quoted field.");
    if (!endedQuote) field += character;
  }
  if (quoted) throw new Error("The CSV has an unfinished quoted field.");
  row.push(field.trim());
  if (row.some(value => value !== "")) rows.push(row);
  return rows;
}

export function parseEmployeeCsv(text, departments, { recovering = false } = {}) {
  const [headers, ...rows] = csvRows(text);
  if (!headers || headers.length !== employeeCsvHeaders.length || new Set(headers).size !== headers.length || employeeCsvHeaders.some(name => !headers.includes(name))) throw new Error(`Use exactly these CSV headers: ${employeeCsvHeaders.join(",")}.`);
  if (!rows.length || rows.length > 100) throw new Error("Import between 1 and 100 employees at a time.");
  const codes = new Set();
  const activeDepartments = new Set((departments ?? []).filter(row => row.is_active !== false && row.is_active !== 0).map(row => row.id));
  return rows.map((values, index) => {
    const fail = message => { throw new Error(`Row ${index + 2}: ${message}`); };
    if (values.length !== headers.length) fail("the number of columns does not match the headers.");
    const fields = Object.fromEntries(headers.map((name, position) => [name, values[position]]));
    for (const [name, maximum] of [["employee_code", 32], ["full_name", 150]]) {
      if (!fields[name] || [...fields[name]].length > maximum || /[\u0000-\u001f\u007f]/.test(fields[name])) fail(`provide a valid ${name} of at most ${maximum} characters.`);
    }
    const email = fields.email.toLowerCase();
    if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || /[\u0000-\u001f\u007f]/.test(email)) fail("provide a valid email address.");
    if (!/^[1-9]\d*$/.test(fields.department_id)) fail("department_id must be an active department ID.");
    const departmentId = Number(fields.department_id);
    if (!Number.isSafeInteger(departmentId) || !recovering && !activeDepartments.has(departmentId)) fail("department_id must match an active department in the reference list.");
    const code = fields.employee_code.normalize("NFKC").toLowerCase();
    if (codes.has(code)) fail("employee_code is repeated in this import.");
    codes.add(code);
    return { employee_code: fields.employee_code, full_name: fields.full_name, email, department_id: departmentId };
  });
}

export async function employeePayloadFingerprint(employees, crypto = globalThis.crypto) {
  if (!crypto?.subtle?.digest) throw new Error("This browser cannot protect import retries. Use a current browser on localhost or HTTPS.");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(employees)));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function browserStorage() {
  try { return globalThis.sessionStorage; } catch { return null; }
}

export class BulkEmployeeImport {
  constructor(staffId, request, { storage = browserStorage(), createIdentifier = randomIdentifier, fingerprint = employeePayloadFingerprint } = {}) {
    if (!Number.isSafeInteger(staffId) || staffId < 1) throw new Error("Sign in before importing employees.");
    this.request = request;
    this.storage = storage;
    this.storageKey = `meal_employee_import_v1:${staffId}`;
    this.createIdentifier = createIdentifier;
    this.fingerprint = fingerprint;
    this.payload = null;
    this.marker = null;
    this.result = null;
    this.inFlight = false;
    this.submitted = false;
    this.storageError = false;
    try {
      const saved = this.storage?.getItem(this.storageKey);
      if (saved) {
        const marker = JSON.parse(saved);
        if (!requestPattern.test(marker.request_id) || !/^[0-9a-f]{64}$/.test(marker.payload_fingerprint) || !Number.isInteger(marker.employee_count) || marker.employee_count < 1 || marker.employee_count > 100) throw new Error("INVALID_IMPORT_MARKER");
        this.marker = marker;
        this.submitted = true;
      }
    } catch { this.storageError = true; }
  }

  async prepare(employees) {
    if (this.inFlight) throw new Error("Wait for this import to finish.");
    if (this.storageError) throw new Error("The previous import could not be recovered. Ask an administrator to check its records before starting another import.");
    if (!Array.isArray(employees) || !employees.length || employees.length > 100) throw new Error("Import between 1 and 100 employees.");
    const payload = employees.map(row => Object.freeze({ ...row }));
    if (new TextEncoder().encode(JSON.stringify({ request_id: "0".repeat(36), employees: payload })).length > maximumBytes) throw new Error("The import is too large. Use a smaller batch.");
    const digest = await this.fingerprint(payload);
    if (this.submitted && (this.marker.payload_fingerprint !== digest || this.marker.employee_count !== payload.length)) throw new Error("An earlier import is not confirmed. Upload or paste the original CSV to retry that same import.");
    if (!this.submitted) this.marker = { request_id: this.createIdentifier(), payload_fingerprint: digest, employee_count: payload.length };
    this.payload = Object.freeze(payload);
    this.result = null;
    return this.payload;
  }

  async submit() {
    if (this.inFlight) return null;
    if (this.result) return this.result;
    if (!this.payload || !this.marker) throw new Error("Preview the original employee CSV before submitting.");
    if (!this.storage) throw new Error("This browser cannot preserve import retries. Enable session storage before importing employees.");
    try { this.storage.setItem(this.storageKey, JSON.stringify(this.marker)); }
    catch { throw new Error("The import could not be saved for recovery. No request was sent."); }
    const wasSubmitted = this.submitted;
    this.inFlight = true;
    this.submitted = true;
    try {
      const result = await this.request("/employees/bulk", { method: "POST", body: { request_id: this.marker.request_id, employees: this.payload.map(row => ({ ...row })) } });
      const positiveId = value => Number.isSafeInteger(value) && value > 0;
      if (!positiveId(result?.batch_id) || typeof result.replayed !== "boolean" || !Array.isArray(result.employees) || result.employees.length !== this.payload.length || result.employees.some(row => !positiveId(row.employee_id) || !positiveId(row.qr_id) || !positiveId(row.email_id)) || new Set(result.employees.map(row => row.employee_id)).size !== this.payload.length) throw new Error("IMPORT_RESULT_UNCONFIRMED");
      this.result = result;
      try { this.storage.removeItem(this.storageKey); } catch {}
      return result;
    } catch (error) {
      if (!wasSubmitted && (error.status === 422 || [400, 409].includes(error.status) && rejectedImports.has(error.code))) {
        this.submitted = false;
        try { this.storage.removeItem(this.storageKey); } catch {}
        throw new Error(`The import was not accepted. ${rejectedImports.get(error.code) ?? "Correct the CSV."} Preview it again before retrying.`);
      }
      throw new Error("The import result is not confirmed. Keep this request and retry the same import. After reloading, upload the original CSV again; do not create a replacement batch.");
    } finally {
      this.inFlight = false;
    }
  }
}

export function employeeImportFor(staffId, request) {
  let operation = imports.get(staffId);
  if (!operation || operation.result) {
    operation = new BulkEmployeeImport(staffId, request);
    imports.set(staffId, operation);
  }
  return operation;
}
