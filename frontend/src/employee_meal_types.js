import { allPages, api } from "./api.js";
import { escape } from "./core.js";
import { NO_DEPARTMENT } from "./facility_departments.js";
import { activeMealTypes, mealTypeCheckboxes, selectedMealTypeIds } from "./meal_types.js";
import { badge, closeModal, empty, errorMessage, loading, modal, notify, table } from "./ui.js";

export const MAX_MEAL_TYPE_EMPLOYEES = 500;

export function matchesSearch(employee, text) {
  const needle = String(text ?? "").trim().toLowerCase();
  if (!needle) return true;
  return [employee.full_name, employee.email, employee.employee_code, employee.department_name ?? NO_DEPARTMENT]
    .some(value => String(value ?? "").toLowerCase().includes(needle));
}

export function selectMatching(rows, text, selected, limit = MAX_MEAL_TYPE_EMPLOYEES) {
  const next = new Set(selected);
  let skipped = 0;
  for (const row of rows.filter(item => matchesSearch(item, text))) {
    if (next.has(row.id)) continue;
    if (next.size >= limit) { skipped += 1; continue; }
    next.add(row.id);
  }
  return { selected: next, skipped };
}

export function mealTypeNames(ids, mealTypes) {
  const names = new Map((mealTypes ?? []).map(row => [Number(row.id), row.name]));
  return ids.map(id => names.get(Number(id)) ?? `#${id}`).join(" + ");
}

export function previewSummary(preview, names) {
  return `${preview.selected} selected · ${preview.changing} will change · ${preview.unchanged} already ${preview.unchanged === 1 ? "has" : "have"} ${names}.`;
}

export function confirmText(preview, names) {
  return `Set meal types to ${names} for ${preview.changing} employee${preview.changing === 1 ? "" : "s"}? Each change is recorded in the audit log.`;
}

function previewTable(preview) {
  if (!preview.examples?.length) return "";
  const more = preview.changing > preview.examples.length ? `<p class="field-help">Showing the first ${preview.examples.length} of ${preview.changing} employees who will change.</p>` : "";
  return `${table(["Employee", "Code", "Current meal types"], preview.examples.map(row => `<tr><td><strong>${escape(row.full_name)}</strong>${row.is_active ? "" : `<span class="cell-subtitle">Inactive</span>`}</td><td class="mono">${escape(row.employee_code)}</td><td>${escape(row.current_meal_types.length ? row.current_meal_types.join(", ") : "None")}</td></tr>`), "Employees who will change")}${more}`;
}

export async function openBulkMealTypes(context, onChange) {
  const dialog = modal("Set meal types", loading(), true);
  const content = dialog.querySelector(".modal-content");
  const owns = () => dialog.open && dialog.querySelector(".modal-content") === content && context.isCurrent();
  let rows;
  try {
    rows = await allPages("/employees");
  } catch (error) {
    if (owns()) content.innerHTML = errorMessage(error);
    return;
  }
  if (!owns()) return;
  if (!rows.length) {
    content.innerHTML = empty("No employees yet", "Import or add employees first.", "people");
    return;
  }
  const mealTypes = activeMealTypes(context.catalog.meal_types ?? []);
  let selected = new Set();
  content.innerHTML = `<p class="muted">Selected employees get exactly the ticked meal types: unticked types are removed. Recorded meals do not change. Up to ${MAX_MEAL_TYPE_EMPLOYEES} employees at a time.</p><form id="bulk-meal-types-form">${mealTypeCheckboxes(mealTypes, [], "Tick at least one meal type.")}<div class="bulk-removal-toolbar"><label class="grow">Search employees<input name="search" type="search" placeholder="Name, email, code or department" maxlength="150" autocomplete="off"></label></div><div class="bulk-removal-toolbar"><button type="button" class="small-button" id="select-matching">Select all matching</button><button type="button" class="small-button" id="clear-selection">Clear</button><strong id="meal-types-count">0 selected</strong></div><div id="meal-types-employees" class="meal-type-employees"></div><div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">Preview changes</button></div></form><div id="meal-types-preview" aria-live="polite"></div>`;
  const form = content.querySelector("#bulk-meal-types-form");
  const search = form.querySelector('[name="search"]');
  const list = form.querySelector("#meal-types-employees");
  const count = form.querySelector("#meal-types-count");
  const errors = form.querySelector("[data-form-error]");
  const preview = content.querySelector("#meal-types-preview");
  const clearPreview = () => { preview.innerHTML = ""; };
  const draw = () => {
    const shown = rows.filter(row => matchesSearch(row, search.value));
    count.textContent = `${selected.size} selected`;
    list.innerHTML = shown.length ? table(["Select", "Employee", "Code", "Department", "Status"], shown.map(row => `<tr><td><input type="checkbox" name="employee_id" value="${escape(row.id)}" aria-label="Select ${escape(row.full_name)}" ${selected.has(row.id) ? "checked" : ""}></td><td><strong>${escape(row.full_name)}</strong><span class="cell-subtitle">${escape(row.email)}</span></td><td class="mono">${escape(row.employee_code)}</td><td>${escape(row.department_name ?? NO_DEPARTMENT)}</td><td>${badge(row.is_active ? "Active" : "Inactive", row.is_active ? "positive" : "neutral")}</td></tr>`), "Employees to update") : '<p class="muted">No employees match this search.</p>';
    list.querySelectorAll('[name="employee_id"]').forEach(input => input.onchange = () => {
      const id = Number(input.value);
      if (input.checked && selected.size >= MAX_MEAL_TYPE_EMPLOYEES) {
        input.checked = false;
        notify(`Select at most ${MAX_MEAL_TYPE_EMPLOYEES} employees. Change this batch first, then select the rest.`, "error");
        return;
      }
      if (input.checked) selected.add(id); else selected.delete(id);
      count.textContent = `${selected.size} selected`;
      clearPreview();
    });
  };
  search.oninput = draw;
  form.querySelectorAll('[name="meal_type_ids"]').forEach(input => input.addEventListener("change", clearPreview));
  form.querySelector("#select-matching").onclick = () => {
    const result = selectMatching(rows, search.value, selected);
    selected = result.selected;
    if (result.skipped) notify(`${MAX_MEAL_TYPE_EMPLOYEES} employees selected; ${result.skipped} more match. Change this batch first, then select the rest.`, "error");
    clearPreview();
    draw();
  };
  form.querySelector("#clear-selection").onclick = () => { selected = new Set(); clearPreview(); draw(); };
  form.onsubmit = async event => {
    event.preventDefault();
    errors.innerHTML = "";
    clearPreview();
    try {
      const mealTypeIds = selectedMealTypeIds(new FormData(form).getAll("meal_type_ids"), "Tick at least one meal type.");
      if (!selected.size) throw new Error("Select at least one employee.");
      const body = { employee_ids: [...selected].sort((left, right) => left - right), meal_type_ids: mealTypeIds };
      const result = await api("/employees/meal-types/preview", { method: "POST", body });
      if (!owns()) return;
      const names = mealTypeNames(mealTypeIds, mealTypes);
      preview.innerHTML = `<div class="detail-section"><h3>Preview</h3><p>${escape(previewSummary(result, names))}</p>${previewTable(result)}${result.changing ? `<div class="form-actions"><button type="button" class="button primary" id="apply-meal-types">Change ${escape(result.changing)} employee${result.changing === 1 ? "" : "s"}</button></div>` : '<p class="muted">Nothing to change.</p>'}</div>`;
      const apply = preview.querySelector("#apply-meal-types");
      if (!apply) return;
      apply.onclick = () => {
        const holder = document.createElement("div");
        holder.className = "inline-confirm";
        holder.innerHTML = `<p>${escape(confirmText(result, names))}</p><div class="form-actions"><button type="button" class="button" data-cancel>Cancel</button><button type="button" class="button primary" data-confirm>Change ${escape(result.changing)} employee${result.changing === 1 ? "" : "s"}</button></div><div data-form-error></div>`;
        apply.replaceWith(holder);
        holder.querySelector("[data-cancel]").onclick = clearPreview;
        holder.querySelector("[data-confirm]").onclick = async event => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            const done = await api("/employees/meal-types", { method: "POST", body: { ...body, expected_changes: result.changing } });
            if (!context.isCurrent()) return;
            closeModal();
            notify(`${done.changed} employee${done.changed === 1 ? "" : "s"} now ${done.changed === 1 ? "has" : "have"} ${names}.`);
            await onChange();
          } catch (error) {
            if (!owns()) return;
            holder.querySelector("[data-form-error]").innerHTML = errorMessage(error);
            button.disabled = false;
          }
        };
      };
    } catch (error) {
      if (owns()) errors.innerHTML = errorMessage(error);
    }
  };
  draw();
}
