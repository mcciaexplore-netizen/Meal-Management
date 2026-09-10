import { api } from "./api.js";
import { escape } from "./core.js";
import { employeeCsvHeaders, employeeImportFor, parseEmployeeCsv } from "./bulk_employees.js";
import { errorMessage, modal, table } from "./ui.js";

export function employeeImportForm(departments, recovering = false) {
  const rows = (departments ?? []).filter(row => row.is_active !== false && row.is_active !== 0);
  return `<p class="muted">Import up to 100 employees. Each receives a personal QR. Bulk emails wait for your approval; this import does not send email.</p>${recovering ? '<div class="alert" role="status">A previous import is not confirmed. Use the original CSV to recover the same request.</div>' : ""}<form id="bulk-employee-form"><label>Upload CSV<input name="csv_file" type="file" accept=".csv,text/csv"></label><label>Or paste CSV<textarea name="csv_text" rows="7" spellcheck="false" placeholder="${escape(employeeCsvHeaders.join(","))}" maxlength="65536"></textarea></label><p class="field-help">Required headers: <span class="mono">${escape(employeeCsvHeaders.join(","))}</span>. Files and pasted data stay in this browser until you choose Import employees.</p><details><summary>Active department IDs</summary>${rows.length ? table(["Department ID", "Department"], rows.map(row => `<tr><td class="mono">${escape(row.id)}</td><td>${escape(row.name)}</td></tr>`), "Department IDs for import") : '<p class="muted">Add an active department in Office settings first.</p>'}</details><div data-import-error></div><div class="form-actions"><button type="submit" class="button">Preview employees</button></div></form><div id="bulk-preview"></div><div id="bulk-import-status" role="status" aria-live="polite"></div>`;
}

export function showEmployeeImport(context, onChange) {
  const operation = employeeImportFor(context.user.staff_id, api);
  const dialog = modal("Import employees", employeeImportForm(context.catalog.departments, operation.submitted), true);
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
  file.onchange = () => { if (file.files?.length) text.value = ""; clearPreview(); };
  text.oninput = () => { file.value = ""; clearPreview(); };
  function drawPreview() {
    const rows = operation.payload ?? [];
    preview.innerHTML = `<div class="detail-section"><h3>${rows.length} employees ready for review</h3>${table(["Code", "Name", "Email", "Department ID"], rows.map(row => `<tr><td>${escape(row.employee_code)}</td><td>${escape(row.full_name)}</td><td>${escape(row.email)}</td><td>${escape(row.department_id)}</td></tr>`), "Employee import preview")}<p class="field-help">Review every row. Existing employee codes are not overwritten. Emails remain pending approval.</p><button type="button" class="button primary" id="confirm-employee-import">${operation.submitted ? "Retry same import" : "Import employees"}</button></div>`;
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
          status.textContent = operation.submitted ? "Import not confirmed. Reuse the same request until its result is known." : "No employees were imported. Correct the CSV and preview it again.";
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
      if (selectedFile?.size > 65536) throw new Error("Choose a CSV smaller than 64 KB.");
      const source = selectedFile ? await selectedFile.text() : text.value;
      const rows = parseEmployeeCsv(source, context.catalog.departments, { recovering: operation.submitted });
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
