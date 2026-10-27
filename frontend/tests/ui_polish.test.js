import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { mealTypeCheckboxes } from "../src/meal_types.js";
import { visitorMealTypeChoices } from "../src/screens.js";

const read = name => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
const rule = (css, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`(?:^|\\n|\\}\\s*)${escaped}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `missing rule ${selector}`);
  return match[1];
};

test("checkboxes and radios never take text-input sizing", async () => {
  const css = await read("styles.css");
  const body = rule(css, 'form input[type="checkbox"], form input[type="radio"]');
  for (const declaration of ["width: 18px", "height: 18px", "min-height: 0", "padding: 0", "margin: 0", "flex: 0 0 auto"]) {
    assert.ok(body.includes(declaration), declaration);
  }
  assert.ok(css.indexOf('form input[type="checkbox"]') > css.indexOf("form input, form select, form textarea {"), "the checkbox rule must come after the text-input rule");
  const label = rule(css, ".role-choices .checkbox");
  assert.match(label, /white-space: nowrap/);
  assert.match(rule(css, ".role-choices legend"), /font-size: 12px/);
});

test("every meal-type checkbox list keeps each label on one line", () => {
  const types = [{ id: 2, name: "Breakfast", is_active: true }, { id: 4, name: "High Tea", is_active: true }, { id: 5, name: "Dinner", is_active: true }];
  for (const html of [mealTypeCheckboxes(types), visitorMealTypeChoices(types, null)]) {
    const labels = html.match(/<label class="checkbox"><input type="checkbox"[^>]*> [^<]+<\/label>/g) ?? [];
    assert.equal(labels.length, 3);
    assert.match(html, /<fieldset class="role-choices meal-type-choices"><legend>/);
  }
});

test("badges stay on one line and the visitor QR details have a layout", async () => {
  const css = await read("styles.css");
  const badge = rule(css, ".badge");
  assert.match(badge, /white-space: nowrap/);
  assert.doesNotMatch(badge, /overflow-wrap: anywhere/);
  assert.match(rule(css, ".detail-grid"), /display: grid/);
  assert.match(rule(css, ".detail-grid > div > span"), /display: block/);
  assert.match(rule(css, ".detail-grid strong"), /overflow-wrap: anywhere/);
  const narrow = css.slice(css.lastIndexOf("@media (max-width: 720px)"));
  assert.match(narrow, /\.detail-grid \{ grid-template-columns: 1fr; \}/);
  assert.ok(css.lastIndexOf("@media (max-width: 720px)") > css.indexOf(".detail-grid {"), "the narrow layout must come after the base rule");
});

test("tables keep phones and meal dates on one line and give labels room", async () => {
  const [screens, access, css] = await Promise.all([read("screens.js"), read("scanner_access.js"), read("styles.css")]);
  assert.match(screens, /<td class="nowrap">\$\{escape\(row\.employee_phone \?\? row\.visitor_phone \?\? "—"\)\}<\/td>/);
  assert.match(screens, /<td class="nowrap">\$\{badge\(row\.kind === "MASTER" \? "Visitor" : "Employee"\)\}<\/td>/);
  assert.match(screens, /<td class="nowrap">\$\{escape\(timestamp\(row\.created_at \?\? row\.queued_at\)\)\}/);
  assert.equal((access.match(/<td class="label-cell">/g) ?? []).length, 2);
  assert.match(rule(css, ".label-cell"), /min-width: 150px/);
});

test("forms inside panels are padded and narrow filters stack", async () => {
  const css = await read("styles.css");
  assert.match(rule(css, ".panel > form.form-grid"), /padding: 0 25px 25px/);
  assert.match(rule(css, ".form-grid > *"), /min-width: 0/);
  assert.match(css, /@media \(max-width: 420px\) \{\n  \.filter-bar > label \{ flex-basis: 100%; \}/);
  assert.match(rule(css, ".catalog-actions"), /flex-wrap: nowrap/);
  assert.match(css, /\.sidebar nav \{ -webkit-mask-image: linear-gradient/);
});

test("the scanner keeps a reachable hidden file input, a styled picker and a full-height background", async () => {
  const css = await read("scan_only.css");
  const upload = rule(css, '.scan-upload > input[type="file"]');
  for (const declaration of ["position: absolute", "width: 1px", "height: 1px", "clip-path: inset(50%)", "opacity: 0"]) assert.ok(upload.includes(declaration), declaration);
  assert.doesNotMatch(upload, /display: none|visibility: hidden/);
  assert.match(rule(css, ".scan-upload:has(> input:focus-visible) > label"), /outline: 3px solid/);
  assert.match(rule(css, ".scan-meal-type select"), /min-height: 48px; padding: 8px 12px; font-size: 18px/);
  assert.match(rule(css, ".scan-only-body"), /min-height: 100dvh/);
  const app = await read("scan_app.js");
  assert.match(app, /<label class="button" for="qr-upload">Upload QR image<\/label><input id="qr-upload" type="file"/);
});
