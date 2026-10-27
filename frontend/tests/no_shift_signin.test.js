import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";
import { LABEL_NOTE } from "../src/scanner_access.js";

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");

test("no screen asks for a waiter's location, selfie or consent", async () => {
  const names = (await readdir(new URL("../src/", import.meta.url))).filter(name => name.endsWith(".js"));
  assert.ok(!names.includes("shift_signin.js") && !names.includes("waiter_shifts.js"));
  for (const name of names) {
    const text = await source(name);
    for (const pattern of [/geolocation/, /getCurrentPosition/, /capture="user"/, /I agree/, /NOTICE_VERSION/, /\/shift/, /waiter-shifts/, /shift-selfies/]) {
      assert.doesNotMatch(text, pattern, `${name} ${pattern}`);
    }
  }
});

test("after activation the scanner opens straight to the meal-type picker", async () => {
  const app = await source("scan_app.js");
  assert.match(app, /if \(!flow\.request && recovery\.request\) flow\.restore\(recovery\.request\);\n    renderScanner\(\);/);
  assert.match(app, /await transport\.activate\([^)]*\)[^;]*;\n      if \(current === version\) await openScanner\(\);/);
  assert.doesNotMatch(app, /renderSignIn|setInterval|Sign out|shift-banner/);
  assert.match(app, /<section class="scan-meal-type" aria-label="Meal type">/);
  assert.match(app, /let selectedMealType = null;/);
  assert.match(app, /function renderActivation\(\) \{\n  const current = \+\+version;\n  dispose\(\);\n  selectedMealType = null;/);
});

test("the admin has no Waiter shifts page and Scanner access explains that names are permanent", async () => {
  const app = await source("app.js");
  assert.doesNotMatch(app, /Waiter shifts|waiterShifts/);
  const access = await source("scanner_access.js");
  assert.match(LABEL_NOTE, /cannot be edited later/);
  assert.match(LABEL_NOTE, /vendor or counter/);
  assert.match(access, /field\("Vendor or counter name", "label", "", "text", 'required maxlength="100"/);
  assert.equal((access.match(/\$\{escape\(LABEL_NOTE\)\}/g) ?? []).length, 2);
  assert.equal((access.match(/table\(\["Vendor or counter"/g) ?? []).length, 2);
});
