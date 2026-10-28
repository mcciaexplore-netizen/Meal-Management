import { api } from "./api.js";
import { escape, query } from "./core.js";
import { EMPLOYEE_XLSX, MAXIMUM_XLSX_BYTES, departmentChoices, employeeCsvHeaders, employeeFileKind, employeeImportFor, parseEmployeeCsvImport, parseEmployeeImport, sampleEmployeeCsv } from "./bulk_employees.js";
import { NO_DEPARTMENT } from "./facility_departments.js";
import { mealTypeCodes } from "./meal_types.js";
import { errorMessage, modal, table } from "./ui.js";

export function employeeImportForm(departments, recovering = false, mealTypes = []) {
  const headers = ["employee_code", "full_name", "email", "company_name", "phone", "department", "meal_types"].join(",");
  const codes = mealTypeCodes(mealTypes).join(", ");
  return `<p class="muted">Import up to 100 employees. Each receives a personal QR. Bulk emails wait for your approval; this import does not send email.</p>${recovering ? '<div class="alert" role="status">A previous import is not confirmed. Use the original file to recover the same request.</div>' : ""}<form id="bulk-employee-form"><label>Upload CSV or Excel<input name="csv_file" type="file" accept=".csv,.xlsx,text/csv,${EMPLOYEE_XLSX}"></label><label>Or paste CSV<textarea name="csv_text" rows="7" spellcheck="false" placeholder="${escape(headers)}" maxlength="65536"></textarea></label><p class="field-help">Required headers: <span class="mono">${escape(employeeCsvHeaders.join(","))}</span>. <span class="mono">meal_types</span> is required on every row: meal type codes separated by <span class="mono">;</span>, for example <span class="mono">BREAKFAST;HIGH-TEA</span>. Valid codes: <span class="mono">${escape(codes || "none active")}</span>. Optional <span class="mono">department</span> column: the department name, or blank for no department. In Excel, use the first sheet with the headers in row 1 and plain values, not formulas. CSV files and pasted data stay in this browser until you choose Import employees; an Excel file is sent to the server only to read it for the preview, and nothing is saved until you import.</p><details><summary>Department names</summary><p class="field-help">${escape(departmentChoices(departments).join(", ") || "None yet. Leave the department blank.")}</p></details><div data-import-error></div><div class="form-actions"><button type="button" class="button" id="employee-sample">Download sample CSV</button><button type="submit" class="button">Preview employees</button></div></form><div id="bulk-preview"></div><div id="bulk-import-status" role="status" aria-live="polite"></div>`;
}

export function ignoredColumnsNote(ignored) {
  return ignored?.length ? `Ignored column: ${ignored.join(", ")}. Departments come from the optional department column.` : "";
}

export function showEmployeeImport(context, onChange) {
  const operation = employeeImportFor(context.user.staff_id, api);
  const dialog = modal("Import employees", employeeImportForm(context.catalog.departments, operation.submitted, context.catalog.meal_types), true);
  const content = dialog.querySelector(".modal-content");
  const ownsDialog = () => dialog.open && dialog.querySelector(".modal-content") === content && context.isCurrent();
  const form = content.querySelector("#bulk-employee-form");
  const file = form.querySelector('[name="csv_file"]');
  const text = form.querySelector('[name="csv_text"]');
  const errors = form.querySelector("[data-import-error]");
  const preview = content.querySelector("#bulk-preview");
  const status = content.querySelector("#bulk-import-status");
  const busy = value => {
    form.querySelectorAll("input,textarea,button").forEach(input => { input.disabled = value; });
    preview.querySelectorAll("button").forEach(button => { button.disabled = value; });
  };
  const clearPreview = () => {
    if (!operation.submitted) preview.innerHTML = "";
  };
  let ignored = [];
  form.querySelector("#employee-sample").onclick = () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([sampleEmployeeCsv(context.catalog.departments, context.catalog.meal_types)], { type: "text/csv" }));
    link.download = "employee-import-sample.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  };
  file.onchange = () => { if (file.files?.length) text.value = ""; clearPreview(); };
  text.oninput = () => { file.value = ""; clearPreview(); };
  function drawPreview() {
    const rows = operation.payload ?? [];
    preview.innerHTML = `<div class="detail-section"><h3>${rows.length} employees ready for review</h3>${ignoredColumnsNote(ignored) ? `<p class="alert" role="status">${escape(ignoredColumnsNote(ignored))}</p>` : ""}${table(["Code", "Name", "Department", "Email", "Company", "Phone"], rows.map(row => `<tr><td>${escape(row.employee_code)}</td><td>${escape(row.full_name)}</td><td>${escape(row.department ?? NO_DEPARTMENT)}</td><td>${escape(row.email)}</td><td>${escape(row.company_name)}</td><td class="nowrap">${escape(row.phone)}</td></tr>`), "Employee import preview")}<p class="field-help">Review every row. Existing employee codes are not overwritten. Emails remain pending approval.</p><button type="button" class="button primary" id="confirm-employee-import">${operation.submitted ? "Retry same import" : "Import employees"}</button></div>`;
    preview.querySelector("button").onclick = async () => {
      if (operation.inFlight) return;
      errors.innerHTML = "";
      busy(true);
      status.textContent = "Registering employees… No emails are being sent.";
      try {
        const result = await operation.submit();
        if (!result) return;
        if (ownsDialog()) {
          form.hidden = true;
          preview.innerHTML = `<div class="alert" role="status"><strong>${result.employees.length} employees ${result.replayed ? "already registered" : "registered"}.</strong><p>Batch ${escape(result.batch_id)}: emails are pending approval. Review them in Email queue before sending.</p><a class="button primary" href="#emails?bulk_batch_id=${escape(result.batch_id)}">Review this batch</a></div>`;
          status.textContent = "No emails were sent by this import.";
        }
        if (context.isCurrent()) await onChange();
      } catch (error) {
        if (ownsDialog()) {
          errors.innerHTML = errorMessage(error);
          status.textContent = operation.submitted ? "Import not confirmed. Reuse the same request until its result is known." : "No employees were imported. Correct the file and preview it again.";
          drawPreview();
        }
      } finally {
        if (ownsDialog()) busy(false);
      }
    };
  }
  form.onsubmit = async event => {
    event.preventDefault();
    if (operation.inFlight) return;
    errors.innerHTML = "";
    busy(true);
    try {
      const selectedFile = file.files?.[0];
      const options = { recovering: operation.submitted, mealTypes: context.catalog.meal_types };
      let parsed;
      if (selectedFile && employeeFileKind(selectedFile) === "xlsx") {
        if (selectedFile.size > MAXIMUM_XLSX_BYTES) throw new Error("Choose an Excel file smaller than 1 MB.");
        const result = await api(`/employees/import-file?${query({ filename: selectedFile.name })}`, { method: "POST", body: new Blob([selectedFile], { type: EMPLOYEE_XLSX }) });
        parsed = parseEmployeeImport(result.rows, context.catalog.departments, options);
      } else {
        if (selectedFile?.size > 65536) throw new Error("Choose a CSV smaller than 64 KB.");
        parsed = parseEmployeeCsvImport(selectedFile ? await selectedFile.text() : text.value, context.catalog.departments, options);
      }
      const rows = parsed.employees;
      ignored = parsed.ignored;
      await operation.prepare(rows);
      if (ownsDialog()) { drawPreview(); status.textContent = "Preview only. No employee or email has been created yet."; }
    } catch (error) {
      if (ownsDialog()) errors.innerHTML = errorMessage(error);
    } finally {
      if (ownsDialog()) busy(false);
    }
  };
  if (operation.payload) drawPreview();
  return operation;
}
