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
  assert.deepEqual((await readdir(join(output, "scanner"))).sort(), ["THIRD_PARTY_NOTICES.txt", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "index.html", "manifest.webmanifest", "scan-app.js", "scan-only.css", "styles.css"]);
});

test("scanner installation uses its own root without credentials and includes correctly sized PNG icons", async () => {
  const manifest = JSON.parse(await readFile(join(output, "scanner", "manifest.webmanifest"), "utf8"));
  assert.equal(manifest.id, "/");
  assert.equal(manifest.start_url, "/");
  assert.equal(manifest.scope, "/");
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.prefer_related_applications, false);
  assert.equal(manifest.short_name, "Meal Scanner");
  for (const [file, size] of [["icon-192.png", 192], ["icon-512.png", 512], ["icon-maskable-512.png", 512], ["apple-touch-icon.png", 180]]) {
    const png = await readFile(join(output, "scanner", file));
    assert.deepEqual([...png.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
    assert.equal(png.readUInt32BE(16), size);
    assert.equal(png.readUInt32BE(20), size);
    if (file !== "apple-touch-icon.png") {
      const icon = manifest.icons.find(item => item.src === `/assets/${file}`);
      assert.equal(icon.sizes, `${size}x${size}`);
      assert.equal(icon.type, "image/png");
      assert.equal(icon.purpose, file.includes("maskable") ? "maskable" : "any");
    }
  }
  const scannerHtml = await readFile(join(output, "scanner", "index.html"), "utf8");
  assert.match(scannerHtml, /rel="manifest" href="\/assets\/manifest.webmanifest"/);
  assert.match(scannerHtml, /rel="apple-touch-icon"/);
  assert.match(scannerHtml, /id="scanner-install"/);
  assert.ok(scannerHtml.indexOf('id="scanner-install"') < scannerHtml.indexOf('id="scan-app"'));
  const adminHtml = await readFile(join(output, "admin", "index.html"), "utf8");
  assert.doesNotMatch(adminHtml, /manifest.webmanifest|scanner-install|apple-touch-icon/);
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
