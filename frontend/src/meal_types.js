import { escape } from "./core.js";

const enabled = row => row.is_active === true || row.is_active === 1;

export function activeMealTypes(rows = []) {
  return rows.filter(enabled);
}

export function mealTypeCheckboxes(rows = [], selectedIds = null, help = "The employee's QR is refused for meal types that are not ticked.") {
  const active = activeMealTypes(rows);
  const chosen = new Set((selectedIds ?? active.map(row => row.id)).map(Number));
  if (!active.length) return '<fieldset class="role-choices"><legend>Valid for meal types</legend><p class="field-help">Add a meal type in Office settings first.</p></fieldset>';
  return `<fieldset class="role-choices meal-type-choices"><legend>Valid for meal types</legend>${active.map(row => `<label class="checkbox"><input type="checkbox" name="meal_type_ids" value="${escape(row.id)}" ${chosen.has(Number(row.id)) ? "checked" : ""}> ${escape(row.name)}</label>`).join("")}<p class="field-help">${escape(help)}</p></fieldset>`;
}

export function selectedMealTypeIds(values, message = "Tick at least one meal type this employee's QR is valid for.") {
  const ids = [...new Set(values.map(Number))];
  if (!ids.length || ids.some(id => !Number.isSafeInteger(id) || id < 1)) throw new Error(message);
  return ids.sort((left, right) => left - right);
}

export function sameMealTypes(left = [], right = []) {
  const a = [...new Set(left.map(Number))].sort((x, y) => x - y);
  const b = [...new Set(right.map(Number))].sort((x, y) => x - y);
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

export function mealTypeIdsFromCodes(text, rows = []) {
  const value = String(text ?? "").trim();
  if (!value) return null;
  const byCode = new Map(activeMealTypes(rows).map(row => [String(row.code).toUpperCase(), Number(row.id)]));
  const ids = value.split(";").map(code => code.trim().toUpperCase()).filter(Boolean).map(code => {
    if (!byCode.has(code)) throw new Error(`meal_types contains an unknown or inactive meal type code: ${code}.`);
    return byCode.get(code);
  });
  if (!ids.length) return null;
  return [...new Set(ids)].sort((left, right) => left - right);
}

export function formatIndiaTime(value) {
  if (typeof value !== "string" || !value) return "";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const parsed = new Date(normalized);
  if (!Number.isFinite(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }).format(parsed);
}

export function mealTypeRejection(code, result, mealTypeName) {
  const name = mealTypeName || "this meal type";
  if (code === "MEAL_TYPE_NOT_ELIGIBLE") return `Not eligible for ${name}.`;
  if (code === "MEAL_TYPE_ALREADY_RECORDED") {
    const time = formatIndiaTime(result?.previous_served_at);
    return time ? `${name} already recorded at ${time} (India time).` : `${name} was already recorded in the last 12 hours.`;
  }
  if (code === "MEAL_TYPE_UNAVAILABLE") return `${name} is no longer offered. Choose another meal type.`;
  if (code === "MASTER_MEAL_TYPE_NOT_ALLOWED") return `This visitor QR is not valid for ${name}.`;
  if (code === "MASTER_MEAL_TYPE_USED_UP") return `All ${name} meals on this visitor QR have been used.`;
  return null;
}

export function visitorMealTypeLines(qr) {
  const rows = Array.isArray(qr?.meal_types) ? qr.meal_types : [];
  return rows.map(row => {
    const name = `${row.name}${row.is_active === false || row.is_active === 0 ? " (inactive)" : ""}`;
    const remaining = Number(row.meals_remaining);
    return remaining > 0 ? `${name}: ${remaining} of ${row.meal_limit} left` : `${name}: used up`;
  });
}

export function chooseMealType(types, id) {
  return (types ?? []).find(row => String(row.id) === String(id)) ?? null;
}

export function mealTypeOptions(types, selected) {
  return `<option value="">Choose meal type…</option>${(types ?? []).map(row => `<option value="${escape(row.id)}" ${selected && String(selected.id) === String(row.id) ? "selected" : ""}>${escape(row.name)}</option>`).join("")}`;
}
