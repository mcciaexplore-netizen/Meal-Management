import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { buildApplications } from "../build.mjs";

let output;

before(async () => {
  output = await mkdtemp(join(tmpdir(), "meal-app-build-"));
  await Promise.all([
    writeFile(join(output, "app.js"), "obsolete admin bundle"),
    writeFile(join(output, "scan-app.js"), "obsolete scanner bundle"),
    writeFile(join(output, "index.html"), "obsolete mixed entry"),
    writeFile(join(output, "scan.html"), "obsolete scanner entry"),
    writeFile(join(output, "preserve.txt"), "Unrelated file")
  ]);
  await buildApplications(output);
});

after(async () => {
  if (output) await rm(output, { recursive: true, force: true });
});

test("build emits separate admin and scanner artifact directories and removes only known mixed outputs", async () => {
  assert.deepEqual((await readdir(output)).sort(), ["admin", "preserve.txt", "scanner"]);
  assert.equal(await readFile(join(output, "preserve.txt"), "utf8"), "Unrelated file");
  assert.deepEqual((await readdir(join(output, "admin"))).sort(), ["THIRD_PARTY_NOTICES.txt", "app.js", "index.html", "styles.css"]);
  assert.deepEqual((await readdir(join(output, "scanner"))).sort(), ["THIRD_PARTY_NOTICES.txt", "index.html", "scan-app.js", "scan-only.css", "styles.css"]);
});

test("each HTML entry references only assets available from its own application", async () => {
  for (const [application, entry] of [["admin", "app.js"], ["scanner", "scan-app.js"]]) {
    const html = await readFile(join(output, application, "index.html"), "utf8");
    const assets = [...html.matchAll(/(?:href|src)="\/assets\/([^"]+)"/g)].map(match => match[1]);
    assert.ok(assets.includes(entry));
    for (const asset of assets) await readFile(join(output, application, asset));
    assert.equal(assets.filter(asset => asset.endsWith(".js")).length, 1);
  }
});

test("scanner bundle contains its own scanner transport without admin login, employee administration, or report routes", async () => {
  const scanner = await readFile(join(output, "scanner", "scan-app.js"), "utf8");
  assert.match(scanner, /\/api\/scanner/);
  assert.match(scanner, /Accepted/);
  assert.match(scanner, /VISITOR_DETAILS/);
  assert.match(scanner, /REQUEST_MISMATCH/);
  assert.doesNotMatch(scanner, /\/api\/auth|\/auth\/login|\/auth\/logout|\/reports\/|\/employees|\/master-qrs|\/email-queue|Sign in to your workspace|Employee registration/);
  const admin = await readFile(join(output, "admin", "app.js"), "utf8");
  assert.match(admin, /\/application/);
  assert.match(admin, /\/auth\/login/);
  assert.doesNotMatch(admin, /\/api\/scanner|Live QR camera|Scan next meal/);
});
