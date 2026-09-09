import { positive } from "./core.js";

export const facilityDepartments = Object.freeze([
  { key: "prototype-production", name: "Prototype Production Facility" },
  { key: "rapid-prototype", name: "Rapid Prototype Centre" },
  { key: "environmental-testing", name: "Environmental Testing" },
  { key: "rubber-polymer", name: "Rubber & Polymer Testing Lab" },
  { key: "cmm-metrology", name: "Large Bed CMM & Metrology Services" },
  { key: "exhibition", name: "Exhibition Centre" },
  { key: "auditorium", name: "Auditorium" },
  { key: "training-seminar", name: "Training & Seminar Hall" }
].map(item => Object.freeze(item)));

const nameKey = value => String(value ?? "").trim().normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
const enabled = row => row.is_active !== false && row.is_active !== 0;

export function employeeDepartmentOptions(rows = []) {
  const choices = [];
  const included = new Set();
  for (const department of facilityDepartments) {
    const key = nameKey(department.name);
    included.add(key);
    const existing = rows.find(row => nameKey(row.name) === key);
    if (existing) {
      if (enabled(existing)) choices.push({ ...existing, name: department.name });
    } else {
      choices.push({ id: "facility:" + department.key, name: department.name });
    }
  }
  for (const row of rows) {
    const key = nameKey(row.name);
    if (enabled(row) && !included.has(key)) {
      choices.push({ ...row });
      included.add(key);
    }
  }
  return choices;
}

export async function resolveEmployeeDepartment(value, { request, refreshCatalog }) {
  if (typeof value !== "string" || !value.startsWith("facility:")) return positive(value, "department");
  const department = facilityDepartments.find(item => "facility:" + item.key === value);
  if (!department) throw new Error("Select a valid department.");
  const findExisting = catalog => {
    if (!Array.isArray(catalog?.departments)) throw new Error("Departments could not be loaded. Please try again.");
    const existing = catalog.departments.find(row => nameKey(row.name) === nameKey(department.name));
    if (!existing) return null;
    if (!enabled(existing)) throw new Error("This department is inactive. Choose an active department.");
    return positive(existing.id, "department");
  };
  const existing = findExisting(await refreshCatalog());
  if (existing !== null) return existing;
  try {
    const created = await request("/catalog/departments", { method: "POST", body: { name: department.name } });
    positive(created?.id, "department");
  } catch (error) {
    if (error.code !== "DEPARTMENT_NAME_EXISTS") throw error;
  }
  const identifier = findExisting(await refreshCatalog());
  if (identifier === null) throw new Error("The department could not be confirmed. Please try again before registering.");
  return identifier;
}
