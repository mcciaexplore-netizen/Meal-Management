import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");

test("navigation and screens hide SUPER_ADMIN exclusive features from ADMIN", async () => {
  const app = await source("app.js");
  assert.match(app, /const isSuperAdmin = user\.roles\.includes\("SUPER_ADMIN"\);/);
  assert.match(app, /if \(!isSuperAdmin\) \{[\s\S]*pages\.delete\("scanner-access"\);[\s\S]*pages\.delete\("settings"\);/);

  const screens = await source("screens.js");
  assert.match(screens, /isSuperAdmin \? '<button class="button" id="set-meal-types">Set meal types<\/button>' : ''/);
  assert.match(screens, /isSuperAdmin \? '<button class="button" type="button" id="export-report">Export to Excel<\/button>' : ''/);
});

test("single visitor QR form forces admin department and blocks admin without department", async () => {
  const screens = await source("screens.js");
  assert.match(screens, /const canCreate = isSuperAdmin \|\| Boolean\(context\.user\.department_id\);/);
  assert.match(screens, /Your administrator account has no department assigned\. Ask a super admin to assign a department before creating visitor QRs\./);
  assert.match(screens, /actionBtn = canCreate \? '<button class="button primary" id="new-master">'/);
  assert.match(screens, /<label>Department<input type="text" value="\$\{escape\(context\.user\.department_name \|\| 'Department'\)\}" readonly>/);
  assert.match(screens, /<input type="hidden" name="department_id" value="\$\{context\.user\.department_id\}">/);
});

test("bulk visitor QRs page blocks admin without department and provides sample Excel download", async () => {
  const page = await source("visitor_batches.js");
  assert.match(page, /blockedFromCreation = !isSuperAdmin && !hasDept;/);
  assert.match(page, /Your administrator account has no department assigned\. Ask a super admin to assign a department before creating visitor QRs\./);
  assert.match(page, /id="visitor-sample-excel"/);
  assert.match(page, /id="visitor-sample-csv"/);
  assert.match(page, /downloadVisitorSample\("xlsx"/);
  assert.match(page, /downloadVisitorSample\("csv"/);
  assert.match(page, /api\(`\/visitor-batches\/sample\.\$\{ext\}`/);
});

test("staff department is optional and can be cleared", async () => {
  const { staffDepartmentField, staffDepartmentId, staffChanges } = await import("../src/office_settings.js");
  const html = staffDepartmentField([{ id: 7, name: "Auditorium", is_active: true }], 7);
  assert.doesNotMatch(html, /<select name="department_id" required/);
  assert.match(html, /<option value="">No department<\/option>/);
  const unexpected = async () => { throw new Error("Unexpected request"); };
  assert.equal(await staffDepartmentId("", { refreshCatalog: unexpected }), null);
  assert.equal(await staffDepartmentId(null, { refreshCatalog: unexpected }), null);
  assert.equal(await staffDepartmentId("7", { refreshCatalog: unexpected }), 7);
  const original = { display_name: "Asha", email: "asha@example.com", roles: ["ADMIN"], department_id: 7 };
  assert.deepEqual(staffChanges(original, { display_name: "Asha", email: "asha@example.com", roles: ["ADMIN"], department_id: null }).profile,
    { display_name: "Asha", email: "asha@example.com", department_id: null });
  assert.equal(staffChanges(original, { display_name: "Asha", email: "asha@example.com", roles: ["ADMIN"], department_id: 7 }).profile, null);
});
