import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { mealTypeRejection, selectedMealTypeIds, visitorMealTypeLines } from "../src/meal_types.js";
import { VISITOR_ALLOWANCE_HELP, visitorMealTypeCell, visitorMealTypeChoices } from "../src/screens.js";

const types = [
  { id: 1, code: "SCANNER-MEAL", name: "Meal", is_active: false },
  { id: 2, code: "BREAKFAST", name: "Breakfast", is_active: true },
  { id: 3, code: "LUNCH", name: "Lunch", is_active: true },
  { id: 4, code: "DINNER", name: "Dinner", is_active: true }
];

test("a new visitor QR is valid for every active meal type by default", () => {
  const html = visitorMealTypeChoices(types, null);
  assert.equal((html.match(/name="meal_type_ids"/g) ?? []).length, 3);
  assert.equal((html.match(/ checked>/g) ?? []).length, 3);
  assert.doesNotMatch(html, /Meal</);
  assert.match(html, /The visitor QR is refused for meal types that are not ticked\./);
});

test("creating another visitor QR keeps the previous QR's still-active types", () => {
  const previous = { meal_types: [{ meal_type_id: 1 }, { meal_type_id: 3 }] };
  const html = visitorMealTypeChoices(types, previous);
  assert.match(html, /value="3" checked>/);
  assert.doesNotMatch(html, /value="2" checked>/);
  assert.doesNotMatch(html, /value="4" checked>/);
});

test("at least one meal type must be ticked", () => {
  assert.throws(() => selectedMealTypeIds([], "Tick at least one meal type this visitor QR is valid for."), /this visitor QR/);
  assert.deepEqual(selectedMealTypeIds(["4", "2"]), [2, 4]);
});

test("the modal explains that Number of people is the allowance per ticked meal type", async () => {
  assert.equal(VISITOR_ALLOWANCE_HELP, "Each ticked meal type allows this many scans.");
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /\$\{VISITOR_ALLOWANCE_HELP\} Number of people is the allowance for each ticked meal type\./);
  assert.match(source, /meal_type_ids: selectedMealTypeIds\(data\.getAll\("meal_type_ids"\)/);
  assert.match(source, /<td>\$\{visitorMealTypeCell\(row\)\}<\/td>/);
  assert.match(source, /<span>Valid for<\/span><strong>\$\{visitorMealTypeCell\(qr\)\}<\/strong>/);
});

test("allowed types show what is left for each", () => {
  const qr = { meal_limit: 5, meals_remaining: 3, meal_types: [
    { name: "Lunch", meal_limit: 5, meals_remaining: 3, is_active: true },
    { name: "Dinner", meal_limit: 5, meals_remaining: 0, is_active: true },
    { name: "High Tea", meal_limit: 5, meals_remaining: 5, is_active: false }
  ] };
  assert.deepEqual(visitorMealTypeLines(qr), ["Lunch: 3 of 5 left", "Dinner: used up", "High Tea (inactive): 5 of 5 left"]);
  assert.equal((visitorMealTypeCell(qr).match(/class="meal-type-line"/g) ?? []).length, 3);
  assert.equal(visitorMealTypeCell({ meal_limit: null }), "—");
  assert.match(visitorMealTypeCell({ meal_limit: 4, meals_remaining: 2, meal_types: [] }), /2 of 4 left/);
  assert.match(visitorMealTypeCell({ meal_limit: 1, meal_types: [{ name: "<b>", meal_limit: 1, meals_remaining: 1 }] }), /&lt;b&gt;/);
});

test("the scanner names the meal type when a visitor QR is refused", () => {
  assert.equal(mealTypeRejection("MASTER_MEAL_TYPE_NOT_ALLOWED", {}, "Dinner"), "This visitor QR is not valid for Dinner.");
  assert.equal(mealTypeRejection("MASTER_MEAL_TYPE_USED_UP", {}, "Lunch"), "All Lunch meals on this visitor QR have been used.");
});
