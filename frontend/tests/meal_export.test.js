import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { downloadMealReport, exportFileName, exportPath } from "../src/meal_export.js";

const form = values => ({ get: name => values[name] ?? "" });

test("the export URL carries the current filters and the file name matches the server", () => {
  assert.equal(exportPath(form({ start: "2026-11-01", end: "2026-11-05" })), "/reports/meals/export?from=2026-11-01&through=2026-11-05");
  assert.equal(exportPath(form({ start: "2026-11-01", end: "2026-11-05", employee_id: "7", department_id: "3", meal_type_id: "2" })),
    "/reports/meals/export?from=2026-11-01&through=2026-11-05&employee_id=7&department_id=3&meal_type_id=2");
  assert.throws(() => exportPath(form({ start: "2026-11-05", end: "2026-11-01" })), /end date/);
  assert.throws(() => exportPath(form({ start: "2026-11-01", end: "2026-11-05", employee_id: "x" })));
  assert.equal(exportFileName("2026-11-01", "2026-11-05"), "meal-report_2026-11-01_to_2026-11-05.xlsx");
});

test("the button is busy while preparing and shows a friendly error", async () => {
  const button = { disabled: false, textContent: "Export to Excel" };
  const holder = [];
  const errors = { innerHTML: "", replaceChildren: node => holder.push(node) };
  globalThis.FormData = class { constructor(values) { this.values = values; } get(name) { return this.values[name] ?? ""; } };
  globalThis.document = { createElement: () => ({ setAttribute() {}, set textContent(value) { this.text = value; }, get textContent() { return this.text; } }) };
  let during;
  await downloadMealReport({ start: "2026-11-01", end: "2026-11-05" }, button, errors, async () => {
    during = [button.disabled, button.textContent];
    throw Object.assign(new Error("No meals match these filters."), { status: 404 });
  });
  assert.deepEqual(during, [true, "Preparing…"]);
  assert.deepEqual([button.disabled, button.textContent], [false, "Export to Excel"]);
  assert.equal(holder[0].text, "No meals match these filters.");
  await downloadMealReport({ start: "2026-11-01", end: "2026-11-05" }, button, errors, async () => { throw new Error("The connection was interrupted. For a scan, retry this same serving to check its result."); });
  assert.equal(holder[1].text, "The connection was interrupted. Export again.");
  let calls = 0;
  button.disabled = true;
  await downloadMealReport({}, button, errors, async () => { calls += 1; });
  assert.equal(calls, 0);
});

test("Meal history has the Export to Excel button wired to the filter form", async () => {
  const screens = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(screens, /type="submit">Apply filters<\/button><button class="button" type="button" id="export-report">Export to Excel<\/button>/);
  assert.match(screens, /downloadMealReport\(form, event\.currentTarget, form\.querySelector\("\[data-form-error\]"\)\)/);
  const source = await readFile(new URL("../src/meal_export.js", import.meta.url), "utf8");
  assert.match(source, /request\(exportPath\(data\), \{ responseType: "blob" \}\)/);
});
