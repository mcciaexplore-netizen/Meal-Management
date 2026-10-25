import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { catalogPanel, catalogUpdatePayload, departmentDeactivationMessage, isSharedRow, splitByStatus, staffChanges } from "../src/office_settings.js";
import { employeeDepartmentOptions } from "../src/facility_departments.js";

const shared = { scanner_id: 3, location_id: 4, meal_type_id: 5 };

test("entries are split into active and inactive lists", () => {
  const rows = [{ id: 1, is_active: true }, { id: 2, is_active: 0 }, { id: 3, is_active: 1 }, { id: 4, is_active: false }];
  const groups = splitByStatus(rows);
  assert.deepEqual(groups.active.map(row => row.id), [1, 3]);
  assert.deepEqual(groups.inactive.map(row => row.id), [2, 4]);
});

test("the shared Meal Scanner entries are recognised for each kind", () => {
  assert.equal(isSharedRow("scanners", { id: 3 }, shared), true);
  assert.equal(isSharedRow("locations", { id: "4" }, shared), true);
  assert.equal(isSharedRow("meal-types", { id: 5 }, shared), true);
  assert.equal(isSharedRow("scanners", { id: 4 }, shared), false);
  assert.equal(isSharedRow("departments", { id: 3 }, shared), false);
  assert.equal(isSharedRow("scanners", { id: 3 }, {}), false);
});

test("active view offers Edit and Deactivate except on protected scanner entries", () => {
  const rows = [{ id: 3, code: "SHARED-SCANNER", name: "Meal Scanner", is_active: true }, { id: 8, code: "C2", name: "Counter 2", is_active: true }, { id: 9, code: "OLD", name: "Old counter", is_active: false }];
  const html = catalogPanel("scanners", rows, shared, "active");
  assert.match(html, /Active \(2\)/);
  assert.match(html, /Inactive \(1\)/);
  assert.match(html, /data-edit="3"/);
  assert.doesNotMatch(html, /data-status="3"/);
  assert.match(html, /data-status="8" data-next="false">Deactivate/);
  assert.match(html, /Meal Scanner<\/span>/);
  assert.doesNotMatch(html, /Old counter/);
  const inactive = catalogPanel("scanners", rows, shared, "inactive");
  assert.match(inactive, /data-status="9" data-next="true">Reactivate/);
  assert.doesNotMatch(inactive, /Counter 2/);
});

test("edit payloads require a name and, where it exists, a bounded code", () => {
  assert.deepEqual(catalogUpdatePayload("departments", { name: "  Auditorium " }), { name: "Auditorium" });
  assert.deepEqual(catalogUpdatePayload("meal-types", { code: " LUNCH ", name: "Lunch" }), { code: "LUNCH", name: "Lunch" });
  assert.throws(() => catalogUpdatePayload("meal-types", { code: "", name: "Lunch" }));
  assert.throws(() => catalogUpdatePayload("meal-types", { code: "x".repeat(33), name: "Lunch" }));
  assert.doesNotThrow(() => catalogUpdatePayload("scanners", { code: "x".repeat(64), name: "Counter" }));
  assert.throws(() => catalogUpdatePayload("locations", { code: "L1", name: "" }));
  assert.throws(() => catalogUpdatePayload("unknown", { name: "x" }));
});

test("department deactivation explains what happens to active employees", () => {
  assert.match(departmentDeactivationMessage("Auditorium", 0), /no longer be offered for new employees/);
  assert.match(departmentDeactivationMessage("Auditorium", 1), /^1 active employee is in Auditorium/);
  const many = departmentDeactivationMessage("Auditorium", 12);
  assert.match(many, /^12 active employees are in Auditorium/);
  assert.match(many, /keep their department, QR codes and meal history/);
});

test("staff edits separate profile, role and email changes", () => {
  const original = { display_name: "Asha Rao", email: "asha@example.com", roles: ["WAITER"] };
  assert.deepEqual(staffChanges(original, { display_name: "Asha Rao", email: "asha@example.com", roles: ["WAITER"] }), { profile: null, roles: null, emailChanged: false });
  const renamed = staffChanges(original, { display_name: "Asha R.", email: "ASHA@example.com", roles: ["WAITER"] });
  assert.deepEqual(renamed.profile, { display_name: "Asha R.", email: "asha@example.com" });
  assert.equal(renamed.emailChanged, false);
  const moved = staffChanges(original, { display_name: "Asha Rao", email: "asha.rao@example.com", roles: ["ADMIN", "WAITER"] });
  assert.equal(moved.emailChanged, true);
  assert.deepEqual(moved.roles, ["ADMIN", "WAITER"]);
  assert.throws(() => staffChanges(original, { display_name: "Asha", email: "asha@example.com", roles: [] }));
  assert.throws(() => staffChanges(original, { display_name: "", email: "asha@example.com", roles: ["WAITER"] }));
  assert.throws(() => staffChanges(original, { display_name: "Asha", email: "not-an-email", roles: ["WAITER"] }));
});

test("employee forms keep an employee's own inactive department but never offer it to others", () => {
  const rows = [{ id: 11, name: "Auditorium", is_active: false }, { id: 12, name: "Night Shift", is_active: false }, { id: 13, name: "Day Shift", is_active: true }];
  const fresh = employeeDepartmentOptions(rows);
  assert.equal(fresh.some(row => row.id === 11 || row.id === 12), false);
  assert.ok(fresh.some(row => row.id === 13));
  const editing = employeeDepartmentOptions(rows, 12);
  assert.ok(editing.some(row => row.id === 12 && row.name === "Night Shift (inactive)"));
  assert.equal(editing.some(row => row.id === 11), false);
  const facility = employeeDepartmentOptions(rows, 11);
  assert.ok(facility.some(row => row.id === 11 && row.name === "Auditorium (inactive)"));
});

test("settings page wires edit, status, usage and staff endpoints with the email warning", async () => {
  const source = await readFile(new URL("../src/office_settings.js", import.meta.url), "utf8");
  assert.match(source, /\/catalog\/\$\{kind\}\/\$\{encodeURIComponent\(row\.id\)\}`, \{ method: "PATCH"/);
  assert.match(source, /\/catalog\/\$\{kind\}\/\$\{encodeURIComponent\(row\.id\)\}\/active/);
  assert.match(source, /\/catalog\/departments\/\$\{encodeURIComponent\(row\.id\)\}\/usage/);
  assert.match(source, /Changing the email changes this person's sign-in email/);
  assert.match(source, /\/staff\/\$\{id\}\/roles/);
  assert.match(source, /Scans for this meal type will be refused/);
  const screens = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(screens, /data-staff-view="inactive"/);
  assert.match(screens, /employeeDepartmentOptions\(catalog\.departments, employee\.department_id\)/);
});
