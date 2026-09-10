import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { loadApplication, scannerUrl } from "../src/application.js";

test("admin navigation uses the scanner origin returned by its own application endpoint", async () => {
  const calls = [];
  const configuration = await loadApplication(async path => {
    calls.push(path);
    return { scanner_url: "http://localhost:8001" };
  });
  assert.deepEqual(calls, ["/application"]);
  assert.deepEqual(configuration, { scanner_url: "http://localhost:8001", max_photo_bytes: 5 * 1024 * 1024 });
  assert.equal(scannerUrl({ scanner_url: "https://scanner.example.test/" }), "https://scanner.example.test");
});

test("application carries the server photo limit and rejects invalid configured values", async () => {
  const configuration = await loadApplication(async () => ({ scanner_url: "https://scanner.example.test", max_photo_bytes: 4000000 }));
  assert.equal(configuration.max_photo_bytes, 4000000);
  for (const max_photo_bytes of [null, true, 0, 1023, 1.5, "4000000", 20 * 1024 * 1024 + 1]) {
    await assert.rejects(loadApplication(async () => ({ scanner_url: "https://scanner.example.test", max_photo_bytes })), /photo upload limit could not be confirmed/);
  }
});

test("invalid scanner configuration cannot silently navigate to admin routes or executable URLs", () => {
  for (const scanner_url of [undefined, "", "/scan", "javascript:alert(1)", "data:text/html,scanner", "https://user:password@example.test", "https://example.test/scan", "https://example.test/?token=secret", "https://example.test/#scan"]) {
    assert.throws(() => scannerUrl({ scanner_url }), /address is not configured/);
  }
});

test("application configuration failures propagate without a hardcoded scanner fallback", async () => {
  await assert.rejects(loadApplication(async () => { throw new Error("Configuration unavailable"); }), /Configuration unavailable/);
});

test("admin scanner links and waiter and legacy redirects share the configured scanner origin", async () => {
  const app = await readFile(new URL("../src/app.js", import.meta.url), "utf8");
  const screens = await readFile(new URL("../src/screens.js", import.meta.url), "utf8");
  assert.match(app, /application = await loadApplication\(api\)/);
  assert.match(app, /href="\$\{escape\(application\.scanner_url\)\}"/);
  assert.equal(app.match(/window\.location\.replace\(application\.scanner_url\)/g)?.length, 2);
  assert.equal(screens.match(/href="\$\{escape\(context\.scanner_url\)\}"/g)?.length, 3);
  assert.doesNotMatch(app + screens, /href="\/scan"|location\.replace\("\/scan"\)/);
});
