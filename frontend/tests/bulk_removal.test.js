import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { bulkRemovalPayload } from "../src/bulk_removal.js";

test("employee bulk removal payloads normalize identifiers and reasons", () => {
  assert.deepEqual(bulkRemovalPayload("employees", [9, "7"], "  Duplicate import  "), {
    employee_ids: [7, 9],
    reason: "Duplicate import"
  });
});

test("bulk removal rejects empty duplicate oversized and invalid selections, and meals entirely", () => {
  for (const values of [[], [7, 7], [true], Array.from({ length: 101 }, (_, index) => index + 1)]) {
    assert.throws(() => bulkRemovalPayload("employees", values, "Cleanup"));
  }
  assert.throws(() => bulkRemovalPayload("employees", [7], " "));
  assert.throws(() => bulkRemovalPayload("meals", [19, 12], "Incorrect servings"));
  assert.throws(() => bulkRemovalPayload("unknown", [7], "Cleanup"));
});

test("Office settings offers audited employee batches but no meal removal", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /Bulk employee removal/);
  assert.match(source, /id="bulk-remove-employees"/);
  assert.match(source, /\/employees\/bulk-remove/);
  assert.match(source, /Recorded meals are permanent and cannot be removed/);
  assert.match(source, /maximum of 100 records/);
  assert.match(source, /complete batch succeeds or fails together/);
  assert.doesNotMatch(source, /bulk-remove-meals|\/meals\/bulk-remove|meal-remove|method: "DELETE", body: \{ reason \} \}\);\n\s*closeModal\(\);\n\s*notify\("Meal/);
  assert.doesNotMatch(source, /\/meals\/\$\{/);
});
