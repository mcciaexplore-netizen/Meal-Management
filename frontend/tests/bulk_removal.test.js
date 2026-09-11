import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { bulkRemovalPayload } from "../src/bulk_removal.js";

test("employee and meal bulk removal payloads normalize identifiers and reasons", () => {
  assert.deepEqual(bulkRemovalPayload("employees", [9, "7"], "  Duplicate import  "), {
    employee_ids: [7, 9],
    reason: "Duplicate import"
  });
  assert.deepEqual(bulkRemovalPayload("meals", [19, "12"], "  Incorrect servings  "), {
    meal_ids: [12, 19],
    reason: "Incorrect servings"
  });
});

test("bulk removal rejects empty duplicate oversized and invalid selections", () => {
  for (const values of [[], [7, 7], [true], Array.from({ length: 101 }, (_, index) => index + 1)]) {
    assert.throws(() => bulkRemovalPayload("employees", values, "Cleanup"));
  }
  assert.throws(() => bulkRemovalPayload("meals", [7], " "));
  assert.throws(() => bulkRemovalPayload("unknown", [7], "Cleanup"));
});

test("Office settings exposes audited employee and meal batch actions", async () => {
  const source = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(source, /Bulk data removal/);
  assert.match(source, /id="bulk-remove-employees"/);
  assert.match(source, /id="bulk-remove-meals"/);
  assert.match(source, /\/employees\/bulk-remove/);
  assert.match(source, /\/meals\/bulk-remove/);
  assert.match(source, /maximum of 100 records/);
  assert.match(source, /complete batch succeeds or fails together/);
});
