import { allPages, api } from "./api.js";
import { dateInput, dateRange, escape, list, optionalExpiry, positive, query, randomIdentifier, timestamp } from "./core.js";
import { badge, closeModal, empty, errorMessage, field, formAction, heading, icon, loading, modal, notify, options, qrCard, qrStatus, selectField, stats, table } from "./ui.js";
import { employeeDepartmentOptions, resolveEmployeeDepartment } from "./facility_departments.js";
import { bindEmployeePhoto, employeePhotoField, validateEmployeePhoto as validatePhoto } from "./employee_photo.js";
import { canPreviewEmail, emailDeliverySettings, emailModeDescription, emailModeLabel, emailStatusDetail } from "./email_delivery.js";

const active = rows => (rows ?? []).filter(row => row.is_active !== false && row.is_active !== 0);
const rowId = row => row.id ?? row.qr_id ?? row.authorization_id;
const action = (name, id, text, style = "small-button") => `<button type="button" class="${style}" data-action="${name}" data-id="${escape(id)}">${escape(text)}</button>`;
const formEnd = label => `<div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">${escape(label)}</button></div>`;

export async function dashboard(target, context) {
  const shortcut = `<a class="button primary" href="${escape(context.scanner_url)}">${icon("scan")} Open Meal Scanner</a>`;
  target.innerHTML = `${heading("TODAY AT A GLANCE", "A good day starts here.", "A clear view of your office meal service.", shortcut)}<div id="dashboard-content">${loading()}</div>`;
  const range = dateRange(dateInput(), dateInput());
  const [totals, recent] = await Promise.all([
    api(`/reports/totals?${query(range)}`),
    api(`/reports/scans?${query({ ...range, outcome: "REJECTED", limit: 5 })}`)
  ]);
  if (!context.isCurrent()) return;
  const groups = totals.by_meal_type ?? [];
  target.querySelector("#dashboard-content").innerHTML = `${stats(totals.totals)}<div class="dashboard-grid"><section class="panel"><div class="panel-heading"><div><h2>Meals by service</h2><p class="muted small">Today, in your local time zone</p></div>${icon("plate")}</div>${groups.length ? `<div class="meal-breakdown">${groups.map(group => `<div><span>${escape(group.name)}</span><strong>${Number(group.meal_count).toLocaleString()}</strong></div>`).join("")}</div>` : empty("Your service day is ready", "Meal totals will appear when the first serving is recorded.", "plate")}<a class="panel-link" href="#reports">Open meal history ${icon("arrow")}</a></section><section class="panel accent-panel"><p class="eyebrow">QUICK ACTIONS</p><h2>Keep the line moving.</h2><p class="muted">Manage employees, check visitor meals, or open the separate scanning app.</p><div class="quick-actions"><a href="#employees">${icon("people")} Manage employees ${icon("arrow")}</a><a href="${escape(context.scanner_url)}">${icon("scan")} Open Meal Scanner ${icon("arrow")}</a><a href="#emails">${icon("mail")} Check email queue ${icon("arrow")}</a></div></section></div><section class="panel"><div class="panel-heading"><div><h2>Recent rejected scans</h2><p class="muted small">Review attempts that did not record a meal.</p></div><a class="text-link" href="#reports?view=rejected">View report ${icon("arrow")}</a></div>${scanTable(list(recent))}</section>`;
}

function employeeForm(catalog, employee = {}) {
  return `<div class="form-grid">${field("Employee code", "employee_code", employee.employee_code, "text", 'required maxlength="32" autocomplete="off"')}${field("Full name", "full_name", employee.full_name, "text", 'required maxlength="150" autocomplete="name"')}${field("Email address", "email", employee.email, "email", 'required maxlength="254" autocomplete="email"')}${selectField("Department", "department_id", employeeDepartmentOptions(catalog.departments), employee.department_id)}</div>`;
}

export async function employees(target, context) {
  target.innerHTML = `${heading("PEOPLE & ACCESS", "Employees", "Personal reusable QRs. A separate record for every meal.", '<button class="button primary" id="new-employee">' + icon("plus") + ' Add employee</button>')}<section class="panel"><form id="employee-filters" class="filter-bar"><label class="grow">Search employees<input name="q" type="search" placeholder="Name, email, or employee code" maxlength="150"></label><label>Status<select name="active"><option value="">All employees</option><option value="true" selected>Active</option><option value="false">Inactive</option></select></label><button class="button" type="submit">Apply</button></form><div id="employee-list">${loading()}</div><div class="pagination" id="employee-pagination"></div></section>`;
  let cursor = null;
  let rows = [];
  let photoEditor = null;
  async function load(append = false) {
    const data = new FormData(target.querySelector("#employee-filters"));
    const result = await api(`/employees?${query({ q: data.get("q"), active: data.get("active"), limit: 30, after_id: append ? cursor : null })}`);
    if (!context.isCurrent()) return;
    rows = append ? rows.concat(list(result)) : list(result);
    cursor = result.next_cursor;
    target.querySelector("#employee-list").innerHTML = rows.length ? table(["Employee", "Code", "Department", "Status", ""], rows.map(employee => `<tr><td><strong>${escape(employee.full_name)}</strong><span class="cell-subtitle">${escape(employee.email)}</span></td><td><span class="mono">${escape(employee.employee_code)}</span></td><td>${escape(employee.department_name ?? context.catalog.departments.find(row => row.id === employee.department_id)?.name ?? "—")}</td><td>${badge(employee.is_active ? "Active" : "Inactive", employee.is_active ? "positive" : "neutral")}</td><td>${action("employee-open", employee.id, "Manage")}</td></tr>`), "Employees") : empty("No employees found", "Add your first employee, or adjust your search filters.", "people");
    target.querySelector("#employee-pagination").innerHTML = `<span>${rows.length} shown</span>${cursor ? '<button class="button" id="more-employees">Load more</button>' : ""}`;
    target.querySelector("#more-employees")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
    target.querySelectorAll('[data-action="employee-open"]').forEach(button => button.onclick = () => employeeDetail(Number(button.dataset.id), context, load));
  }
  formAction(target.querySelector("#employee-filters"), () => load());
  target.querySelector("#new-employee").onclick = () => {
    photoEditor?.dispose();
    const dialog = modal("Register an employee", `<p class="muted">Registration creates a personal QR and queues its email automatically.</p><form id="employee-register">${employeeForm(context.catalog)}${employeePhotoField()}${formEnd("Register employee")}</form>`, true);
    const form = dialog.querySelector("form");
    const photoInput = bindEmployeePhoto(dialog);
    photoEditor = photoInput;
    formAction(form, async data => {
      const photo = photoInput.photo ?? data.get("photo");
      validatePhoto(photo);
      photoInput.setBusy(true);
      try {
        const departmentId = await resolveEmployeeDepartment(data.get("department_id"), { request: api, refreshCatalog: context.refreshCatalog });
        const result = await api("/employees", { method: "POST", body: { employee_code: data.get("employee_code"), full_name: data.get("full_name"), email: data.get("email"), department_id: departmentId } });
        const stillOpen = dialog.contains(form);
        photoInput.dispose();
        if (stillOpen) closeModal();
        if (photo?.size) {
          try { await api(`/employees/${result.employee_id}/photo`, { method: "POST", body: photo }); }
          catch (error) { notify(`Employee registered; photo was not saved. ${error.message}`, "error"); }
        }
        notify("Employee registered. QR email queued.");
        if (!context.isCurrent()) return;
        await load();
        if (stillOpen && context.isCurrent()) await employeeDetail(result.employee_id, context, load);
      } finally {
        photoInput.setBusy(false);
      }
    });
  };
  await load();
  return () => photoEditor?.dispose();
}

export async function employeeDetail(id, context, onChange) {
  const dialog = modal("Employee details", loading(), true);
  const content = dialog.querySelector(".modal-content");
  const ownsDialog = () => dialog.open && dialog.querySelector(".modal-content") === content;
  try {
    const [employee, qrResponse, queue, deliverySettings] = await Promise.all([
      api(`/employees/${id}`),
      api(`/employees/${id}/qr`).then(qr => ({ qr })).catch(error => ({ error })),
      api(`/email-queue?${query({ employee_id: id, limit: 10 })}`),
      api("/email-settings")
    ]);
    if (!ownsDialog() || !context.isCurrent()) return;
    const emailSettings = emailDeliverySettings(deliverySettings);
    const qr = qrResponse.qr;
    const noQr = qrResponse.error?.status === 404 || ["QR_NOT_FOUND", "ACTIVE_QR_NOT_FOUND"].includes(qrResponse.error?.code);
    dialog.querySelector(".modal-content").innerHTML = `<div class="employee-detail"><div><div class="detail-identity"><span class="large-avatar">${escape(employee.full_name.slice(0, 1))}</span><div><h3>${escape(employee.full_name)}</h3><p class="muted small">${escape(employee.employee_code)} · ${employee.is_active ? "Active" : "Inactive"}</p></div></div><form id="employee-edit">${employeeForm(context.catalog, employee)}${formEnd("Save changes")}</form><div class="detail-section"><h3>Selfie photo</h3>${employee.selfie_object_key ? `<img class="employee-photo" src="/api/employees/${id}/photo" alt="${escape(employee.full_name)} employee photo">` : '<p class="muted small">No photo uploaded.</p>'}<form id="employee-photo"><label>Upload a photo<input type="file" name="photo" accept="image/jpeg,image/png" required></label>${formEnd("Save photo")}</form></div><div class="detail-section"><h3>Employee access</h3><p class="muted small">${employee.is_active ? "Deactivation prevents future employee meals. Existing meal history is retained." : "Reactivate this employee to enable eligible QR meals."}</p><button class="button ${employee.is_active ? "danger-outline" : ""}" id="employee-status">${employee.is_active ? "Deactivate employee" : "Reactivate employee"}</button></div></div><div><section class="inset-panel"><h3>Personal employee QR</h3>${qr ? qrCard(qr) : noQr ? empty("No active QR", "Issue a replacement to restore QR access.", "qr") : errorMessage(qrResponse.error)}<div class="stack-actions">${qr?.svg ? '<button class="button" id="resend-qr">' + icon("mail") + ' Queue email resend</button>' : ""}<button class="button" id="replace-qr">${qr ? "Replace QR" : "Issue QR"}</button>${qr && !qr.revoked_at ? '<button class="button danger-outline" id="revoke-qr">Revoke QR</button>' : ""}</div><p class="field-help">Treat this QR as a private credential. Replacing it invalidates the previous QR.</p></section><section class="detail-section"><h3>Email delivery</h3><p class="field-help">${escape(emailModeLabel(emailSettings))}</p>${emailTable(list(queue), true, emailSettings)}</section></div></div>`;
    formAction(dialog.querySelector("#employee-edit"), async data => {
      const departmentId = await resolveEmployeeDepartment(data.get("department_id"), { request: api, refreshCatalog: context.refreshCatalog });
      await api(`/employees/${id}`, { method: "PATCH", body: { employee_code: data.get("employee_code"), full_name: data.get("full_name"), email: data.get("email"), department_id: departmentId } });
      notify("Employee updated.");
      await onChange();
    });
    formAction(dialog.querySelector("#employee-photo"), async data => {
      const photo = data.get("photo");
      validatePhoto(photo);
      await api(`/employees/${id}/photo`, { method: "POST", body: photo });
      notify("Private employee photo saved.");
      await employeeDetail(id, context, onChange);
    });
    dialog.querySelector("#employee-status").onclick = () => confirmAction(employee.is_active ? "Deactivate employee" : "Reactivate employee", employee.is_active ? `Deactivate ${employee.full_name}? They will no longer be eligible for employee meals.` : `Reactivate ${employee.full_name}?`, async () => {
      await api(`/employees/${id}${employee.is_active ? "" : "/activate"}`, { method: employee.is_active ? "DELETE" : "POST" });
      notify(employee.is_active ? "Employee deactivated." : "Employee reactivated.");
      await onChange();
      await employeeDetail(id, context, onChange);
    });
    dialog.querySelector("#resend-qr")?.addEventListener("click", async event => {
      event.currentTarget.disabled = true;
      try {
        await api(`/employees/${id}/qr/resend`, { method: "POST" });
        notify("The same active QR has been queued for email.");
        await employeeDetail(id, context, onChange);
      } catch (error) { notify(error.message, "error"); event.target.disabled = false; }
    });
    dialog.querySelector("#replace-qr").onclick = () => expiryDialog(qr ? "Replace employee QR" : "Issue employee QR", async expiresAt => {
      await api(`/employees/${id}/qr/replace`, { method: "POST", body: { expires_at: expiresAt } });
      notify("Employee QR issued and email queued.");
      await employeeDetail(id, context, onChange);
    });
    dialog.querySelector("#revoke-qr")?.addEventListener("click", () => revokeDialog(async reason => {
      await api(`/employees/${id}/qr/revoke`, { method: "POST", body: { reason } });
      notify("Employee QR revoked.");
      await employeeDetail(id, context, onChange);
    }));
    bindEmailPreviews(dialog);
  } catch (error) {
    if (ownsDialog() && context.isCurrent()) content.innerHTML = errorMessage(error);
  }
}

function confirmAction(title, description, callback) {
  const dialog = modal(title, `<p>${escape(description)}</p><form>${formEnd("Confirm")}</form>`);
  formAction(dialog.querySelector("form"), async () => { await callback(); });
}

function expiryDialog(title, callback) {
  const dialog = modal(title, `<p class="muted">Replacing a QR revokes the previous credential. Leave expiry blank for no scheduled expiry.</p><form>${field("Expiry, local time", "expires_at", "", "datetime-local")}${formEnd("Issue QR")}</form>`);
  formAction(dialog.querySelector("form"), async data => callback(optionalExpiry(data.get("expires_at"))));
}

function revokeDialog(callback) {
  const dialog = modal("Revoke QR", `<p class="muted">This credential will no longer approve meals. Revocation cannot be undone.</p><form>${field("Reason", "reason", "", "text", 'required maxlength="255"')}${formEnd("Revoke QR")}</form>`);
  formAction(dialog.querySelector("form"), async data => callback(data.get("reason")));
}

export async function masters(target, context) {
  target.innerHTML = `${heading("VISITOR MEAL ACCESS", "Admin-office master QR", "One reusable QR for visitor meals.", '<button class="button primary" id="new-master">' + icon("plus") + ' Issue master QR</button>')}<div class="info-banner">${icon("qr")}<span>In Meal Scanner, scan this QR and enter Company Name, Name, Email, and Phone. Each completed submission records one visitor meal.</span></div><section class="panel"><div id="master-list">${loading()}</div><div class="pagination" id="master-pagination"></div></section>`;
  let rows = [];
  let cursor = null;
  async function load(append = false) {
    const result = await api(`/master-qrs?${query({ limit: 30, after_id: append ? cursor : null })}`);
    if (!context.isCurrent()) return;
    rows = append ? rows.concat(list(result)) : list(result);
    cursor = result.next_cursor;
    target.querySelector("#master-list").innerHTML = rows.length ? table(["Credential", "Status", "Issued", "Expiry", ""], rows.map(row => `<tr><td><strong>Office master QR ${escape(rowId(row))}</strong></td><td>${qrStatus(row)}</td><td>${escape(timestamp(row.issued_at))}</td><td>${escape(row.expires_at ? timestamp(row.expires_at) : "No expiry")}</td><td>${action("open-master", rowId(row), "Manage")}</td></tr>`), "Master credentials") : empty("No master QR issued", "Issue an office credential before approving visitor meals.", "qr");
    target.querySelectorAll('[data-action="open-master"]').forEach(button => button.onclick = () => detail(Number(button.dataset.id)));
    target.querySelector("#master-pagination").innerHTML = `<span>${rows.length} credentials shown</span>${cursor ? '<button class="button" id="more-master">Load more</button>' : ""}`;
    target.querySelector("#more-master")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
  }
  async function detail(id) {
    const dialog = modal("Office master QR", loading());
    try {
      const qr = await api(`/master-qrs/${id}`);
      if (!dialog.open) return;
      dialog.querySelector(".modal-content").innerHTML = `${qrCard(qr)}<p class="field-help">Keep this reusable QR private. Meal Scanner records one visitor meal after entering the visitor's company, name, email, and phone.</p><div class="stack-actions"><button class="button" id="replace-master">${qr.revoked_at ? "Issue new master QR" : "Replace master QR"}</button>${!qr.revoked_at ? '<button class="button danger-outline" id="revoke-master">Revoke master QR</button>' : ""}<a class="button primary" href="${escape(context.scanner_url)}">Open Meal Scanner</a></div>`;
      dialog.querySelector("#replace-master").onclick = () => expiryDialog(qr.revoked_at ? "Issue new master QR" : "Replace master QR", async expiresAt => {
        const result = await api(qr.revoked_at ? "/master-qrs" : `/master-qrs/${id}/replace`, { method: "POST", body: { expires_at: expiresAt } });
        notify(qr.revoked_at ? "New master QR issued." : "Master QR replaced. Previous credential revoked.");
        await load();
        await detail(result.qr_id ?? result.id);
      });
      dialog.querySelector("#revoke-master")?.addEventListener("click", () => revokeDialog(async reason => {
        await api(`/master-qrs/${id}/revoke`, { method: "POST", body: { reason } });
        closeModal();
        notify("Master QR revoked.");
        await load();
      }));
    } catch (error) { if (dialog.open) dialog.querySelector(".modal-content").innerHTML = errorMessage(error); }
  }
  target.querySelector("#new-master").onclick = () => expiryDialog("Issue master QR", async expiresAt => {
    const result = await api("/master-qrs", { method: "POST", body: { expires_at: expiresAt } });
    notify("Master QR issued.");
    await load();
    await detail(result.qr_id ?? result.id);
  });
  await load();
}

export async function visitors(target, context) {
  const [masterData, approvals] = await Promise.all([allPages("/master-qrs"), allPages("/visitor-authorizations?status=pending")]);
  if (!context.isCurrent()) return;
  const masterRows = list(masterData).filter(row => !row.revoked_at && (!row.expires_at || new Date(row.expires_at) > new Date())).map(row => ({ ...row, id: rowId(row), name: `Master QR ${rowId(row)}` }));
  target.innerHTML = `${heading("ADMIN AUTHORIZATION", "Welcome your visitors", "Approve a specific serving, then let the assigned waiter scan the master QR.")}<div class="two-columns"><section class="panel padded"><h2>Authorize a visitor serving</h2><p class="muted small">Your signed-in administrator identity is recorded with this approval.</p><form id="visitor-form"><div class="form-grid">${field("Visitor / party lead", "visitor_name", "", "text", 'required maxlength="150"')}${field("Organization", "visitor_organization", "", "text", 'maxlength="150"')}${selectField("Master QR", "master_qr_id", masterRows)}${selectField("Serving waiter", "waiter_id", active(context.catalog.waiters), "", { label: "display_name" })}${selectField("Scanner / serving counter", "scanner_code", active(context.catalog.scanners), "", { value: "code" })}${selectField("Meal type", "meal_type_id", active(context.catalog.meal_types))}${field("Meal quantity", "quantity", 1, "number", 'required min="1" max="1000" step="1"')}${field("Approval expiry, local time", "expires_at", "", "datetime-local")}</div>${field("Visit purpose", "visit_purpose", "", "text", 'maxlength="255"')}<p class="field-help">One meal record is created for each person in the approved quantity.</p>${formEnd("Authorize serving")}</form></section><section class="panel"><div class="panel-heading"><div><h2>Pending approvals</h2><p class="muted small">Ready for the assigned waiter.</p></div>${icon("check")}</div>${approvalTable(list(approvals))}</section></div>`;
  let requestId = randomIdentifier();
  formAction(target.querySelector("#visitor-form"), async data => {
    const body = { request_id: requestId, master_qr_id: positive(data.get("master_qr_id"), "master QR"), waiter_id: positive(data.get("waiter_id"), "waiter"), scanner_code: data.get("scanner_code"), meal_type_id: positive(data.get("meal_type_id"), "meal type"), quantity: positive(data.get("quantity"), "quantity"), visitor_name: data.get("visitor_name"), visitor_organization: data.get("visitor_organization") || null, visit_purpose: data.get("visit_purpose") || null };
    if (data.get("expires_at")) body.expires_at = optionalExpiry(data.get("expires_at"));
    await api("/visitor-authorizations", { method: "POST", body });
    requestId = randomIdentifier();
    notify("Visitor meals authorized for the assigned waiter.");
    await context.reload();
  });
}

function approvalTable(rows) {
  if (!rows.length) return empty("No pending approvals", "Visitor servings you authorize will appear here.", "check");
  return table(["Visitor", "Meals", "Waiter", "Expires"], rows.map(row => `<tr><td><strong>${escape(row.visitor_name)}</strong><span class="cell-subtitle">${escape(row.visitor_organization ?? "")}</span></td><td>${escape(row.quantity)}</td><td>${escape(row.waiter_name ?? row.waiter_id)}</td><td>${escape(timestamp(row.expires_at))}</td></tr>`), "Visitor authorizations");
}

export function mealTable(rows) {
  if (!rows.length) return empty("No meals in this period", "Try a different date range or record a serving.", "plate");
  return table(["Meal", "Employee / visitor", "Company", "Email", "Phone", "Category", "Service", "Waiter", "Location", "Date & time"], rows.map(row => `<tr><td class="mono">${escape(row.meal_id ?? row.id)}</td><td><strong>${escape(row.employee_name ?? row.full_name ?? row.visitor_name ?? "Visitor")}</strong><span class="cell-subtitle">${escape(row.employee_code ?? "")}</span></td><td>${escape(row.visitor_company_name ?? row.visitor_organization ?? "—")}</td><td>${escape(row.visitor_email ?? "—")}</td><td>${escape(row.visitor_phone ?? "—")}</td><td>${badge(row.kind === "MASTER" ? "Visitor" : "Employee")}</td><td>${escape(row.meal_type_name ?? row.meal_type_code ?? row.meal_type_id)}</td><td>${escape(row.waiter_name ?? row.waiter_id)}</td><td>${escape(row.location_name ?? row.location_code ?? row.location_id)}</td><td class="nowrap">${escape(timestamp(row.served_at))}</td></tr>`), "Meal records");
}

function scanTable(rows) {
  if (!rows.length) return empty("No rejected scans", "Rejected attempts for this period will appear here.", "check");
  return table(["Date & time", "Reason", "Waiter", "Scanner", "Location"], rows.map(row => `<tr><td class="nowrap">${escape(timestamp(row.received_at ?? row.created_at))}</td><td>${badge(row.rejection_code ?? row.code ?? "Rejected", "danger")}</td><td>${escape(row.waiter_name ?? row.staff_name ?? row.actor_name ?? row.staff_id ?? "—")}</td><td>${escape(row.scanner_code ?? row.scanner_id ?? "—")}</td><td>${escape(row.location_name ?? row.location_code ?? row.location_id ?? "—")}</td></tr>`), "Rejected scans");
}

export async function reports(target, context) {
  const month = new Date();
  month.setDate(1);
  let view = location.hash.includes("view=rejected") ? "rejected" : "meals";
  target.innerHTML = `${heading("HISTORY & ACCOUNTABILITY", "Meal history", "Filter individual meal records and review rejected scan attempts.")}<form id="report-filters" class="panel filter-bar"><label>From<input name="start" type="date" value="${dateInput(month)}" required></label><label>Through<input name="end" type="date" value="${dateInput()}" required></label><label>Employee ID <span class="optional">Optional</span><input name="employee_id" type="number" min="1" step="1" placeholder="All employees and visitors"></label><button class="button primary" type="submit">Apply filters</button><div data-form-error></div></form><p class="field-help">Dates include the full local day. Totals cover all employees and visitors in the selected dates.</p><div id="report-totals">${stats()}</div><section class="panel"><div class="tabs" role="tablist" aria-label="Report type"><button role="tab" data-report="meals">Meal records</button><button role="tab" data-report="rejected">Rejected scans</button></div><div id="report-results">${loading()}</div><div class="pagination" id="report-pagination"></div></section>`;
  let rows = [];
  let cursor = null;
  let loadVersion = 0;
  async function load(append = false) {
    const version = ++loadVersion;
    const data = new FormData(target.querySelector("#report-filters"));
    const range = dateRange(data.get("start"), data.get("end"));
    const employeeId = data.get("employee_id") ? positive(data.get("employee_id"), "employee ID") : null;
    target.querySelectorAll("[data-report]").forEach(button => { button.setAttribute("aria-selected", button.dataset.report === view ? "true" : "false"); button.classList.toggle("active", button.dataset.report === view); });
    const endpoint = view === "meals" ? "/reports/meals" : "/reports/scans";
    const params = { ...range, limit: 40, after_id: append ? cursor : null, employee_id: view === "meals" ? employeeId : null, outcome: view === "rejected" ? "REJECTED" : null };
    const [result, totals] = await Promise.all([api(`${endpoint}?${query(params)}`), append ? Promise.resolve(null) : api(`/reports/totals?${query(range)}`)]);
    if (!context.isCurrent() || version !== loadVersion) return;
    rows = append ? rows.concat(list(result)) : list(result);
    cursor = result.next_cursor;
    if (totals) target.querySelector("#report-totals").innerHTML = stats(totals.totals);
    target.querySelector("#report-results").innerHTML = view === "meals" ? mealTable(rows) : scanTable(rows);
    target.querySelector("#report-pagination").innerHTML = `<span>${rows.length} records shown</span>${cursor ? '<button class="button" id="more-reports">Load more</button>' : ""}`;
    target.querySelector("#more-reports")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
  }
  formAction(target.querySelector("#report-filters"), () => load());
  target.querySelectorAll("[data-report]").forEach(button => button.onclick = () => { view = button.dataset.report; load().catch(error => notify(error.message, "error")); });
  await load();
}

export function emailTable(rows, compact = false, settings = null) {
  if (!rows.length) return empty("No email messages", "Register an employee or queue a QR resend.", "mail");
  const previews = rows.some(row => canPreviewEmail(row, settings));
  const columns = compact ? ["Queue ID", "Status", "Queued"] : ["Queue ID", "Recipient", "Status", "Queued"];
  if (previews) columns.push("");
  return table(columns, rows.map(row => `<tr><td class="mono">${escape(row.id ?? row.email_id)}</td>${compact ? "" : `<td><strong>${escape(row.recipient_email ?? row.recipient)}</strong><span class="cell-subtitle">${escape(row.subject ?? "Employee meal QR")}</span></td>`}<td>${badge(row.status, row.status === "SENT" ? "positive" : row.status === "FAILED" ? "danger" : "neutral")}<span class="cell-subtitle">${escape(emailStatusDetail(row.status))}</span></td><td>${escape(timestamp(row.created_at ?? row.queued_at))}</td>${previews ? `<td>${canPreviewEmail(row, settings) ? action("email-preview", row.id ?? row.email_id, "Preview") : ""}</td>` : ""}</tr>`), "Email queue");
}

function bindEmailPreviews(target) {
  target.querySelectorAll('[data-action="email-preview"]').forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      const html = await api(`/email-queue/${positive(button.dataset.id, "email")}/preview`, { responseType: "text" });
      const dialog = modal("Private email preview", '<p class="muted small">Development preview only. Opening this preview does not send an email.</p><iframe class="email-preview" title="Employee QR email preview" sandbox=""></iframe>', true);
      dialog.querySelector("iframe").srcdoc = html;
    } catch (error) { notify(error.message, "error"); }
    finally { button.disabled = false; }
  });
}

export async function emails(target, context) {
  target.innerHTML = `${heading("EMPLOYEE QR DELIVERY", "Email queue", "Review QR email status and the configured delivery mode.", '<button class="button" id="refresh-email">Refresh queue</button>')}<div class="info-banner">${icon("mail")}<div id="email-mode">Checking email delivery settings…</div></div><section class="panel"><div id="email-list">${loading()}</div><div class="pagination" id="email-pagination"></div></section>`;
  let rows = [];
  let cursor = null;
  async function load(append = false) {
    const [result, configuration] = await Promise.all([
      api(`/email-queue?${query({ limit: 30, after_id: append ? cursor : null })}`),
      api("/email-settings"),
    ]);
    if (!context.isCurrent()) return;
    const delivery = emailDeliverySettings(configuration);
    rows = append ? rows.concat(list(result)) : list(result);
    cursor = result.next_cursor;
    target.querySelector("#email-mode").innerHTML = `<strong>${escape(emailModeLabel(delivery))}</strong><p class="small">${escape(emailModeDescription(delivery))}</p>`;
    target.querySelector("#email-list").innerHTML = emailTable(rows, false, delivery);
    target.querySelector("#email-pagination").innerHTML = `<span>${rows.length} messages shown</span>${cursor ? '<button class="button" id="more-email">Load more</button>' : ""}`;
    target.querySelector("#more-email")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
    bindEmailPreviews(target);
  }
  target.querySelector("#refresh-email").onclick = () => load().catch(error => notify(error.message, "error"));
  await load();
}

export async function settings(target, context) {
  const configs = [
    { key: "departments", title: "Departments", rows: context.catalog.departments, fields: field("Department name", "name", "", "text", 'required maxlength="100"') },
    { key: "meal-types", title: "Meal types", rows: context.catalog.meal_types, fields: field("Code", "code", "", "text", 'required maxlength="32"') + field("Name", "name", "", "text", 'required maxlength="100"') },
    { key: "locations", title: "Serving locations", rows: context.catalog.locations, fields: field("Code", "code", "", "text", 'required maxlength="32"') + field("Name", "name", "", "text", 'required maxlength="150"') },
    { key: "scanners", title: "Scanning counters", rows: context.catalog.scanners, fields: field("Scanner code", "code", "", "text", 'required maxlength="64"') + field("Name", "name", "", "text", 'required maxlength="150"') + selectField("Location", "location_id", active(context.catalog.locations)) }
  ];
  target.innerHTML = `${heading("OFFICE SETUP", "Service settings", "Set up departments, meal services, locations, and registered scanning counters.")}<div class="settings-grid">${configs.map(config => `<section class="panel"><div class="panel-heading"><h2>${config.title}</h2><button class="small-button" data-add-catalog="${config.key}">Add</button></div>${config.rows?.length ? `<ul class="catalog-list">${config.rows.map(row => `<li><span><strong>${escape(row.name)}</strong>${row.code ? `<small>${escape(row.code)}</small>` : ""}</span>${badge(row.is_active ? "Active" : "Inactive", row.is_active ? "positive" : "neutral")}</li>`).join("")}</ul>` : empty("Nothing configured yet", "Add an entry to get this workspace ready.", "settings")}</section>`).join("")}</div><section class="panel"><div class="panel-heading"><div><h2>Staff accounts</h2><p class="muted small">Workspace access for administrators and waiters.</p></div><button class="button" id="new-staff">${icon("plus")} Add staff</button></div><div id="staff-list">${loading()}</div><div class="pagination" id="staff-pagination"></div></section>`;
  target.querySelectorAll("[data-add-catalog]").forEach(button => button.onclick = () => {
    const config = configs.find(item => item.key === button.dataset.addCatalog);
    const dialog = modal(`Add to ${config.title.toLowerCase()}`, `<form>${config.fields}${formEnd("Save")}</form>`);
    formAction(dialog.querySelector("form"), async data => {
      const body = Object.fromEntries(data);
      if (body.location_id) body.location_id = positive(body.location_id, "location");
      await api(`/catalog/${config.key}`, { method: "POST", body });
      closeModal();
      notify("Office setting added.");
      await context.refreshCatalog();
      await context.reload();
    });
  });
  let staffRows = [];
  let staffCursor = null;
  async function loadStaff(append = false) {
    const result = await api(`/staff?${query({ limit: 30, after_id: append ? staffCursor : null })}`);
    if (!context.isCurrent()) return;
    staffRows = append ? staffRows.concat(list(result)) : list(result);
    staffCursor = result.next_cursor;
    target.querySelector("#staff-list").innerHTML = staffRows.length ? table(["Staff member", "Roles", "Status", ""], staffRows.map(row => `<tr><td><strong>${escape(row.display_name)}</strong><span class="cell-subtitle">${escape(row.email)}</span></td><td>${(row.roles ?? []).map(role => badge(role)).join(" ")}</td><td>${badge(row.is_active ? "Active" : "Disabled", row.is_active ? "positive" : "neutral")}</td><td>${(row.staff_id ?? row.id) !== context.user.staff_id ? action("staff-status", row.staff_id ?? row.id, row.is_active ? "Disable" : "Enable") : badge("You")}</td></tr>`), "Staff accounts") : empty("No staff accounts", "Add an account to give a waiter access.", "people");
    target.querySelector("#staff-pagination").innerHTML = `<span>${staffRows.length} accounts shown</span>${staffCursor ? '<button class="button" id="more-staff">Load more</button>' : ""}`;
    target.querySelector("#more-staff")?.addEventListener("click", () => loadStaff(true).catch(error => notify(error.message, "error")));
    target.querySelectorAll('[data-action="staff-status"]').forEach(button => button.onclick = () => {
      const account = staffRows.find(row => Number(row.staff_id ?? row.id) === Number(button.dataset.id));
      confirmAction(account.is_active ? "Disable staff account" : "Enable staff account", `${account.is_active ? "Disable" : "Enable"} access for ${account.display_name}?`, async () => {
        await api(`/staff/${account.staff_id ?? account.id}/active`, { method: "PATCH", body: { is_active: !account.is_active } });
        closeModal();
        notify("Staff account updated.");
        await context.refreshCatalog();
        await loadStaff();
      });
    });
  }
  target.querySelector("#new-staff").onclick = () => {
    const dialog = modal("Create a staff account", `<p class="muted small">Choose a strong, unique password and share it privately with the staff member.</p><form>${field("Display name", "display_name", "", "text", 'required maxlength="150" autocomplete="name"')}${field("Email address", "email", "", "email", 'required maxlength="254" autocomplete="off"')}${field("Password", "password", "", "password", 'required minlength="12" maxlength="1024" autocomplete="new-password"')}<label>Workspace role<select name="role"><option value="WAITER">Waiter</option><option value="ADMIN">Administrator</option></select></label>${formEnd("Create account")}</form>`);
    formAction(dialog.querySelector("form"), async data => {
      await api("/staff", { method: "POST", body: { display_name: data.get("display_name"), email: data.get("email"), password: data.get("password"), roles: [data.get("role")] } });
      closeModal();
      notify("Staff account created.");
      await context.refreshCatalog();
      await loadStaff();
    });
  };
  await loadStaff();
}
