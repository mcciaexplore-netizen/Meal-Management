import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { MAX_MEAL_TYPE_EMPLOYEES, confirmText, matchesSearch, mealTypeNames, previewSummary, selectMatching } from "../src/employee_meal_types.js";
import { mealTypeCheckboxes } from "../src/meal_types.js";

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
const types = [{ id: 2, code: "BREAKFAST", name: "Breakfast", is_active: true }, { id: 4, code: "HIGH-TEA", name: "High Tea", is_active: true }];
const people = [
  { id: 1, full_name: "Asha Rao", email: "asha@mcciapune.in", employee_code: "E-1", department_name: "Exhibition Centre", is_active: true },
  { id: 2, full_name: "Ravi Kulkarni", email: "ravi@mcciapune.in", employee_code: "E-2", department_name: null, is_active: false },
  { id: 3, full_name: "Sneha Patil", email: "sneha@mcciapune.in", employee_code: "E-3", department_name: "Auditorium", is_active: true },
];

test("search matches name, email, code and department, including No department", () => {
  assert.deepEqual(people.filter(row => matchesSearch(row, "  RAVI ")).map(row => row.id), [2]);
  assert.deepEqual(people.filter(row => matchesSearch(row, "e-3")).map(row => row.id), [3]);
  assert.deepEqual(people.filter(row => matchesSearch(row, "no department")).map(row => row.id), [2]);
  assert.deepEqual(people.filter(row => matchesSearch(row, "")).map(row => row.id), [1, 2, 3]);
});

test("select all matching adds only matches, keeps earlier picks and stops at 500", () => {
  const first = selectMatching(people, "mcciapune", new Set());
  assert.deepEqual([...first.selected], [1, 2, 3]);
  const kept = selectMatching(people, "Sneha", new Set([1]));
  assert.deepEqual([...kept.selected].sort(), [1, 3]);
  assert.equal(MAX_MEAL_TYPE_EMPLOYEES, 500);
  const many = Array.from({ length: 503 }, (_, index) => ({ ...people[0], id: index + 1 }));
  const capped = selectMatching(many, "", new Set());
  assert.equal(capped.selected.size, 500);
  assert.equal(capped.skipped, 3);
});

test("preview and confirmation wording", () => {
  const names = mealTypeNames([2, 4], types);
  assert.equal(names, "Breakfast + High Tea");
  assert.equal(previewSummary({ selected: 120, changing: 95, unchanged: 25 }, names), "120 selected · 95 will change · 25 already have Breakfast + High Tea.");
  assert.equal(previewSummary({ selected: 1, changing: 0, unchanged: 1 }, names), "1 selected · 0 will change · 1 already has Breakfast + High Tea.");
  assert.equal(confirmText({ changing: 95 }, names), "Set meal types to Breakfast + High Tea for 95 employees? Each change is recorded in the audit log.");
  assert.equal(confirmText({ changing: 1 }, names), "Set meal types to Breakfast + High Tea for 1 employee? Each change is recorded in the audit log.");
});

test("the dialog previews first and confirms with the previewed count", async () => {
  const dialog = await source("employee_meal_types.js");
  assert.match(dialog, /api\("\/employees\/meal-types\/preview", \{ method: "POST", body \}\)/);
  assert.match(dialog, /api\("\/employees\/meal-types", \{ method: "POST", body: \{ \.\.\.body, expected_changes: result\.changing \} \}\)/);
  assert.match(dialog, /mealTypeCheckboxes\(mealTypes, \[\], "Tick at least one meal type\."\)/);
  assert.match(dialog, /clearPreview/);
  const screens = await source("screens.js");
  assert.match(screens, /id="set-meal-types">Set meal types<\/button>/);
  assert.match(screens, /openBulkMealTypes\(context, load\)/);
});

test("Add employee pre-ticks no meal types while editing keeps the employee's own", async () => {
  const screens = await source("screens.js");
  assert.match(screens, /mealTypeCheckboxes\(catalog\.meal_types, employee\.id \? employee\.meal_type_ids \?\? null : \[\]\)/);
  assert.match(screens, /const mealTypeIds = selectedMealTypeIds\(data\.getAll\("meal_type_ids"\)\);\n\s+const result = await api\("\/employees", \{ method: "POST"/);
  const none = mealTypeCheckboxes(types, []);
  assert.doesNotMatch(none, /checked/);
  assert.equal(mealTypeCheckboxes(types, [4]).match(/checked/g).length, 1);
});
