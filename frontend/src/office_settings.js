import { api } from "./api.js";
import { escape } from "./core.js";
import { badge, closeModal, empty, field, formAction, modal, notify, selectField } from "./ui.js";
import { employeeDepartmentOptions, resolveEmployeeDepartment } from "./facility_departments.js";

export const catalogKinds = Object.freeze({
  departments: { title: "Departments", singular: "department", code: null, nameLimit: 100, shared: null },
  "meal-types": { title: "Meal types", singular: "meal type", code: 32, nameLimit: 100, shared: null },
  locations: { title: "Serving locations", singular: "serving location", code: 32, nameLimit: 150, shared: "location_id" },
  scanners: { title: "Scanning counters", singular: "scanning counter", code: 64, nameLimit: 150, shared: "scanner_id" }
});

export const isActive = row => row.is_active === true || row.is_active === 1;

export function splitByStatus(rows = []) {
  return { active: rows.filter(isActive), inactive: rows.filter(row => !isActive(row)) };
}

export function isSharedRow(kind, row, shared = {}) {
  const column = catalogKinds[kind]?.shared;
  return Boolean(column && shared?.[column] !== null && shared?.[column] !== undefined && Number(shared[column]) === Number(row.id));
}

export function catalogUpdatePayload(kind, values) {
  const config = catalogKinds[kind];
  if (!config) throw new Error("Unknown setting.");
  const name = String(values.name ?? "").trim();
  if (!name || name.length > config.nameLimit) throw new Error(`Enter a name of up to ${config.nameLimit} characters.`);
  if (config.code === null) return { name };
  const code = String(values.code ?? "").trim();
  if (!code || code.length > config.code) throw new Error(`Enter a code of up to ${config.code} characters.`);
  return { code, name };
}

export function departmentDeactivationMessage(name, activeEmployees) {
  const count = Number(activeEmployees) || 0;
  if (count === 0) return `Deactivate ${name}? It will no longer be offered for new employees.`;
  const people = count === 1 ? "1 active employee is" : `${count} active employees are`;
  return `${people} in ${name}. They keep their department, QR codes and meal history. Deactivating only stops ${name} being offered for new employees and imports.`;
}

export function staffChanges(original, values) {
  const display_name = String(values.display_name ?? "").trim();
  const email = String(values.email ?? "").trim().toLowerCase();
  const roles = [...new Set(values.roles ?? [])].sort();
  if (!display_name || display_name.length > 150) throw new Error("Enter a display name of up to 150 characters.");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error("Enter a valid email address.");
  if (!roles.length) throw new Error("Choose at least one role.");
  const currentRoles = [...(original.roles ?? [])].sort();
  const originalDept = original.department_id ?? null;
  const newDept = values.department_id === undefined ? originalDept : (values.department_id ? Number(values.department_id) : null);
  const deptChanged = values.department_id !== undefined && newDept !== originalDept;
  const nameChanged = display_name !== original.display_name;
  const emailChanged = email !== String(original.email ?? "").toLowerCase();
  const profileChanged = nameChanged || emailChanged || deptChanged;
  let profile = null;
  if (profileChanged) {
    profile = { display_name, email };
    if (values.department_id !== undefined) {
      profile.department_id = newDept;
    }
  }
  return {
    profile,
    roles: roles.join(",") !== currentRoles.join(",") ? roles : null,
    emailChanged
  };
}

function rowHtml(kind, row, shared) {
  const protectedRow = isSharedRow(kind, row, shared);
  const actions = [`<button type="button" class="small-button" data-edit="${escape(row.id)}">Edit</button>`];
  if (isActive(row) && !protectedRow) actions.push(`<button type="button" class="small-button" data-status="${escape(row.id)}" data-next="false">Deactivate</button>`);
  if (!isActive(row)) actions.push(`<button type="button" class="small-button" data-status="${escape(row.id)}" data-next="true">Reactivate</button>`);
  const tags = [protectedRow ? badge("Meal Scanner", "warning") : "", isActive(row) ? "" : badge("Inactive")].join(" ");
  return `<li><span><strong>${escape(row.name)}</strong>${row.code ? `<small>${escape(row.code)}</small>` : ""}${row.location_name ? `<small>${escape(row.location_name)}</small>` : ""}</span><span class="catalog-actions">${tags}${actions.join("")}</span></li>`;
}

export function catalogPanel(kind, rows, shared, view = "active") {
  const config = catalogKinds[kind];
  const groups = splitByStatus(rows);
  const shown = groups[view] ?? groups.active;
  const tabs = ["active", "inactive"].map(name => `<button type="button" class="${name === view ? "active" : ""}" data-view="${name}" aria-pressed="${name === view}">${name === "active" ? "Active" : "Inactive"} (${groups[name].length})</button>`).join("");
  const list = shown.length
    ? `<ul class="catalog-list">${shown.map(row => rowHtml(kind, row, shared)).join("")}</ul>`
    : empty(view === "active" ? "Nothing active" : "Nothing inactive", view === "active" ? "Add an entry to get this workspace ready." : "Deactivated entries appear here with a Reactivate button.", "settings");
  return `<div class="panel-heading"><h2>${escape(config.title)}</h2><button class="small-button" data-add-catalog="${kind}">Add</button></div><div class="status-filter" role="group" aria-label="${escape(config.title)} status">${tabs}</div>${list}`;
}

function editDialog(kind, row, shared, done) {
  const config = catalogKinds[kind];
  const protectedRow = isSharedRow(kind, row, shared);
  const codeField = config.code === null ? "" : field("Code", "code", row.code, "text", `required maxlength="${config.code}" ${protectedRow ? "readonly" : ""}`);
  const notes = [
    protectedRow ? '<p class="field-help">The Meal Scanner uses this entry, so its code is fixed and it cannot be deactivated. You can rename it.</p>' : "",
    kind === "scanners" && !protectedRow ? '<p class="field-help">Staff who enter this counter code must use the new code after you change it. Meal history keeps pointing at the same counter.</p>' : ""
  ].join("");
  const dialog = modal(`Edit ${config.singular}`, `<form>${codeField}${field("Name", "name", row.name, "text", `required maxlength="${config.nameLimit}"`)}${notes}<div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">Save changes</button></div></form>`);
  formAction(dialog.querySelector("form"), async data => {
    await api(`/catalog/${kind}/${encodeURIComponent(row.id)}`, { method: "PATCH", body: catalogUpdatePayload(kind, Object.fromEntries(data)) });
    closeModal();
    notify(`${config.title.slice(0, -1)} updated.`);
    await done();
  });
}

async function statusDialog(kind, row, next, done) {
  const config = catalogKinds[kind];
  let message = next ? `Reactivate ${row.name}? It will be offered again for new choices.` : `Deactivate ${row.name}? It stays in history and reports but is no longer offered for new choices.`;
  let confirm = false;
  if (kind === "departments" && !next) {
    const usage = await api(`/catalog/departments/${encodeURIComponent(row.id)}/usage`);
    message = departmentDeactivationMessage(row.name, usage.active_employees);
    confirm = Number(usage.active_employees) > 0;
  }
  if (kind === "meal-types" && !next) message += " Scans for this meal type will be refused.";
  const dialog = modal(next ? `Reactivate ${config.singular}` : `Deactivate ${config.singular}`, `<p>${escape(message)}</p><form><div data-form-error></div><div class="form-actions"><button type="submit" class="button ${next ? "primary" : "danger-outline"}">${next ? "Reactivate" : "Deactivate"}</button></div></form>`);
  formAction(dialog.querySelector("form"), async () => {
    const body = kind === "departments" ? { is_active: next, confirm } : { is_active: next };
    await api(`/catalog/${kind}/${encodeURIComponent(row.id)}/active`, { method: "PATCH", body });
    closeModal();
    notify(next ? "Reactivated." : "Deactivated.");
    await done();
  });
}

export function bindCatalogPanel(section, kind, getRows, shared, refresh) {
  let view = "active";
  const render = () => {
    section.innerHTML = catalogPanel(kind, getRows(), shared(), view);
    section.querySelectorAll("[data-view]").forEach(button => button.onclick = () => { view = button.dataset.view; render(); });
    section.querySelectorAll("[data-edit]").forEach(button => button.onclick = () => {
      const row = getRows().find(item => String(item.id) === button.dataset.edit);
      if (row) editDialog(kind, row, shared(), async () => { await refresh(); render(); });
    });
    section.querySelectorAll("[data-status]").forEach(button => button.onclick = () => {
      const row = getRows().find(item => String(item.id) === button.dataset.status);
      if (row) statusDialog(kind, row, button.dataset.next === "true", async () => { await refresh(); render(); }).catch(error => notify(error.message, "error"));
    });
  };
  render();
  return render;
}

export const staffDepartmentField = (departments, selected = "") =>
  selectField("Department", "department_id", employeeDepartmentOptions(departments ?? [], selected || null), selected ?? "", { required: false, placeholder: "No department" });

export async function staffDepartmentId(value, context) {
  if (value === null || value === undefined || value === "") return null;
  return resolveEmployeeDepartment(value, { request: api, refreshCatalog: context.refreshCatalog });
}

export function staffEditDialog(account, context, done) {
  const isSuperAdmin = (account.roles ?? []).includes("SUPER_ADMIN");
  const isInteractive = !account.is_scanner && !(account.roles ?? []).includes("WAITER");
  const roleField = isInteractive
    ? `<label>Workspace role<select name="role"><option value="ADMIN" ${!isSuperAdmin ? "selected" : ""}>Administrator</option><option value="SUPER_ADMIN" ${isSuperAdmin ? "selected" : ""}>Super Administrator</option></select></label>`
    : `<input type="hidden" name="role" value="${account.roles?.[0] || 'WAITER'}">`;
  const departmentField = isInteractive
    ? staffDepartmentField(context.catalog?.departments, account.department_id ?? "")
    : "";
  const dialog = modal("Edit staff account", `<form>${field("Display name", "display_name", account.display_name, "text", 'required maxlength="150" autocomplete="off"')}${field("Email address", "email", account.email, "email", 'required maxlength="254" autocomplete="off"')}<p class="alert" role="status" data-email-warning hidden>Changing the email changes this person's sign-in email. They will be signed out everywhere and must sign in with the new address.</p>${roleField}${departmentField}<div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">Save changes</button></div></form>`);
  const form = dialog.querySelector("form");
  const warning = form.querySelector("[data-email-warning]");
  form.querySelector('[name="email"]').addEventListener("input", event => {
    warning.hidden = event.target.value.trim().toLowerCase() === String(account.email ?? "").toLowerCase();
  });
  formAction(form, async data => {
    let deptId = undefined;
    if (isInteractive) {
      deptId = await staffDepartmentId(data.get("department_id"), context);
    }
    const role = data.get("role") || account.roles?.[0] || "ADMIN";
    const changes = staffChanges(account, {
      display_name: data.get("display_name"),
      email: data.get("email"),
      roles: [role],
      department_id: deptId,
    });
    const id = encodeURIComponent(account.staff_id ?? account.id);
    if (changes.profile) await api(`/staff/${id}`, { method: "PATCH", body: changes.profile });
    if (changes.roles) await api(`/staff/${id}/roles`, { method: "PATCH", body: { roles: changes.roles } });
    closeModal();
    notify(changes.emailChanged ? "Staff account updated. They must sign in again with the new email." : "Staff account updated.");
    await done();
  });
}
