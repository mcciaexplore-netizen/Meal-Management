import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { parseEmployeeCsv } from "../src/bulk_employees.js";
import { chooseMealType, formatIndiaTime, mealTypeCheckboxes, mealTypeIdsFromCodes, mealTypeOptions, mealTypeRejection, sameMealTypes, selectedMealTypeIds } from "../src/meal_types.js";

const types = [
  { id: 1, code: "SCANNER-MEAL", name: "Meal", is_active: false },
  { id: 2, code: "BREAKFAST", name: "Breakfast", is_active: true },
  { id: 3, code: "LUNCH", name: "Lunch", is_active: true },
  { id: 4, code: "DINNER", name: "Dinner", is_active: 1 }
];

test("employee eligibility checkboxes default to all active types and keep an existing selection", () => {
  const fresh = mealTypeCheckboxes(types);
  assert.equal((fresh.match(/checked/g) ?? []).length, 3);
  assert.doesNotMatch(fresh, /value="1"/);
  const existing = mealTypeCheckboxes(types, [3]);
  assert.match(existing, /value="3" checked/);
  assert.doesNotMatch(existing, /value="2" checked/);
  assert.deepEqual(selectedMealTypeIds(["4", "3", "3"]), [3, 4]);
  assert.throws(() => selectedMealTypeIds([]), /Tick at least one meal type/);
  assert.equal(sameMealTypes([3, 4], [4, 3]), true);
  assert.equal(sameMealTypes([3], [3, 4]), false);
});

test("CSV meal type codes map to active ids and reject unknown or inactive codes", () => {
  assert.deepEqual(mealTypeIdsFromCodes("lunch; DINNER", types), [3, 4]);
  assert.equal(mealTypeIdsFromCodes("", types), null);
  assert.throws(() => mealTypeIdsFromCodes("LUNCH;SUPPER", types), /SUPPER/);
  assert.throws(() => mealTypeIdsFromCodes("SCANNER-MEAL", types), /SCANNER-MEAL/);
  const departments = [{ id: 7, name: "Auditorium", is_active: true }];
  const base = "employee_code,full_name,email,company_name,phone,department";
  assert.throws(() => parseEmployeeCsv(`${base}\nE-1,Asha,asha@example.com,Example,+91 98765 43210,Auditorium\n`, departments, { mealTypes: types }), /Add a meal_types column/);
  const typed = parseEmployeeCsv(`${base},meal_types\nE-1,Asha,asha@example.com,Example,+91 98765 43210,Auditorium,LUNCH;DINNER\nE-2,Ravi,ravi@example.com,Example,+91 98765 43211,Auditorium,BREAKFAST\n`, departments, { mealTypes: types });
  assert.deepEqual(typed[0].meal_type_ids, [3, 4]);
  assert.deepEqual(typed[1].meal_type_ids, [2]);
  assert.throws(() => parseEmployeeCsv(`${base},meal_types\nE-1,Asha,asha@example.com,Example,+91 98765 43210,Auditorium,SUPPER\n`, departments, { mealTypes: types }), /Row 2: .*SUPPER/);
  assert.throws(() => parseEmployeeCsv(`${base},other\nE-1,Asha,asha@example.com,Example,+91 98765 43210,Auditorium,x\n`, departments, { mealTypes: types }), /with an optional department column/);
});

test("scanner refusals name the meal type and show the earlier time in India time", () => {
  assert.equal(mealTypeRejection("MEAL_TYPE_NOT_ELIGIBLE", {}, "Dinner"), "Not eligible for Dinner.");
  assert.equal(mealTypeRejection("MEAL_TYPE_ALREADY_RECORDED", { previous_served_at: "2026-10-26T02:45:00Z" }, "Breakfast"), "Breakfast already recorded at 08:15 (India time).");
  assert.equal(mealTypeRejection("MEAL_TYPE_ALREADY_RECORDED", {}, "Breakfast"), "Breakfast was already recorded in the last 12 hours.");
  assert.equal(mealTypeRejection("QR_EXPIRED", {}, "Lunch"), null);
  assert.equal(formatIndiaTime("2026-10-25T18:30:00Z"), "00:00");
  assert.equal(formatIndiaTime("2026-10-25T18:29:00"), "23:59");
});

test("the scanner starts with no meal type and keeps the waiter's choice", async () => {
  assert.match(mealTypeOptions(types.slice(1), null), /<option value="">Choose meal type…<\/option>/);
  assert.doesNotMatch(mealTypeOptions(types.slice(1), null), /selected/);
  assert.match(mealTypeOptions(types.slice(1), { id: 3 }), /value="3" selected>Lunch/);
  assert.equal(chooseMealType(types, "4").name, "Dinner");
  assert.equal(chooseMealType(types, ""), null);
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.match(source, /let selectedMealType = null;/);
  assert.match(source, /active\(\) && Boolean\(selectedMealType\)/);
  assert.match(source, /id="meal-current"/);
  assert.doesNotMatch(source, /default_start|default_end|timeOfDay|suggest/i);
  const css = await readFile(new URL("../src/scan_only.css", import.meta.url), "utf8");
  assert.match(css, /\.scan-meal-current \{[^}]*font-size: 34px/);
});

test("meal history offers a meal type filter and an admin correction with a reason", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /<option value="">All meal types<\/option>/);
  assert.match(source, /Change meal type/);
  assert.match(source, /The original stays in the audit log; nothing is deleted/);
  assert.match(source, /\/meal-type`, \{ method: "POST"/);
  assert.match(source, /Corrected · recorded as/);
  assert.match(source, /\/employees\/\$\{id\}\/meal-types`, \{ method: "PATCH"/);
});
