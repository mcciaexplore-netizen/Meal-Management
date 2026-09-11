import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { mountScannerInstall, scannerInstallHelp, ScannerInstallController } from "../src/scanner_install.js";

function fixture(options = {}) {
  const window = new EventTarget();
  const media = new EventTarget();
  media.matches = options.standalone ?? false;
  window.matchMedia = query => { assert.equal(query, "(display-mode: standalone)"); return media; };
  window.isSecureContext = options.secureContext ?? true;
  const values = options.values ?? new Map();
  const storage = options.storage ?? { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  const changes = [];
  const navigator = options.navigator ?? {};
  const controller = new ScannerInstallController({ window, navigator, storage, onChange: state => changes.push(state) });
  return { controller, window, navigator, media, storage, values, changes };
}

function installEvent(window, outcome = "accepted", prompt) {
  const event = new Event("beforeinstallprompt", { cancelable: true });
  event.calls = 0;
  event.prompt = () => { event.calls += 1; return prompt ? prompt() : Promise.resolve({ outcome }); };
  event.userChoice = Promise.resolve({ outcome });
  window.dispatchEvent(event);
  return event;
}

test("ordinary browsers see a nonblocking installation offer without any native prompt", () => {
  const { controller, window } = fixture();
  assert.equal(controller.state.visible, true);
  assert.equal(controller.state.canInstall, false);
  const event = installEvent(window);
  assert.equal(event.defaultPrevented, true);
  assert.equal(event.calls, 0);
  assert.equal(controller.state.canInstall, true);
  controller.dispose();
});

test("only an explicit install action prompts the browser and an accepted choice hides the offer", async () => {
  const { controller, window, values } = fixture();
  const event = installEvent(window);
  assert.equal(await controller.install(), "accepted");
  assert.equal(event.calls, 1);
  assert.equal(controller.state.visible, false);
  assert.equal(controller.installed, false);
  assert.equal(values.size, 1);
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 1);
  controller.dispose();
});

test("declining the browser installation does not claim installation or prompt again this session", async () => {
  const { controller, window, values } = fixture();
  const event = installEvent(window, "dismissed");
  assert.equal(await controller.install(), "dismissed");
  assert.equal(controller.installed, false);
  assert.equal(controller.state.visible, false);
  const later = installEvent(window);
  assert.equal(later.defaultPrevented, true);
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 1);
  assert.equal(later.calls, 0);
  const reloaded = fixture({ values });
  assert.equal(reloaded.controller.state.visible, false);
  controller.dispose();
  reloaded.controller.dispose();
});

test("Not now hides the reminder across scanner reloads within the same session", () => {
  const { controller, values, window } = fixture();
  controller.dismiss();
  assert.equal(controller.state.visible, false);
  const event = installEvent(window);
  assert.equal(controller.state.canInstall, false);
  assert.equal(event.calls, 0);
  const reloaded = fixture({ values });
  assert.equal(reloaded.controller.state.visible, false);
  const newSession = fixture();
  assert.equal(newSession.controller.state.visible, true);
  controller.dispose();
  reloaded.controller.dispose();
  newSession.controller.dispose();
});

test("standalone Android and Apple launches never offer installation", () => {
  for (const options of [{ standalone: true }, { navigator: { standalone: true } }]) {
    const { controller, window } = fixture(options);
    assert.equal(controller.state.visible, false);
    const event = installEvent(window);
    assert.equal(event.defaultPrevented, false);
    assert.equal(controller.state.canInstall, false);
    controller.dispose();
  }
});

test("appinstalled hides the banner and releases a deferred prompt", () => {
  const { controller, window } = fixture();
  installEvent(window);
  window.dispatchEvent(new Event("appinstalled"));
  assert.equal(controller.installed, true);
  assert.equal(controller.state.visible, false);
  assert.equal(controller.deferred, null);
  controller.dispose();
});

test("switching to standalone display hides an already visible offer", () => {
  const { controller, media, changes } = fixture();
  media.matches = true;
  media.dispatchEvent(new Event("change"));
  assert.equal(controller.state.visible, false);
  assert.equal(changes.at(-1).visible, false);
  controller.dispose();
});

test("iPhone and desktop-mode iPad show Safari home-screen steps", async () => {
  for (const navigator of [{ userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)" }, { userAgent: "Mozilla/5.0 (Macintosh)", platform: "MacIntel", maxTouchPoints: 5 }]) {
    const { controller } = fixture({ navigator });
    assert.equal(await controller.install(), null);
    assert.equal(controller.state.helpOpen, true);
    assert.match(controller.state.help, /Safari.*Share.*Add to Home Screen.*Add/);
    assert.equal(controller.state.visible, true);
    controller.dispose();
  }
});

test("unsupported browsers offer honest manual instructions without pretending to install", async () => {
  const { controller } = fixture({ navigator: { userAgent: "Firefox" } });
  assert.equal(await controller.install(), null);
  assert.equal(controller.installed, false);
  assert.equal(controller.state.helpOpen, true);
  assert.match(controller.state.help, /If these options are unavailable/);
  assert.match(controller.state.help, /keep scanning here/);
  controller.dispose();
});

test("insecure pages request the HTTPS link and never trigger a native installation prompt", async () => {
  const { controller, window } = fixture({ secureContext: false });
  const event = installEvent(window);
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 0);
  assert.match(controller.state.help, /HTTPS link/);
  controller.dispose();
});

test("blocked storage cannot break scanner installation controls or in-memory dismissal", () => {
  const storage = { getItem() { throw new Error("Blocked"); }, setItem() { throw new Error("Blocked"); } };
  const { controller } = fixture({ storage });
  assert.equal(controller.state.visible, true);
  assert.doesNotThrow(() => controller.dismiss());
  assert.equal(controller.state.visible, false);
  controller.dispose();
});

test("the same native event cannot be consumed twice while awaiting a browser response", async () => {
  const { controller, window } = fixture();
  let resolve;
  const event = installEvent(window, "accepted", () => new Promise(done => { resolve = done; }));
  const pending = controller.install();
  assert.equal(event.calls, 1);
  assert.equal(controller.state.pending, true);
  assert.equal(controller.state.canInstall, false);
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 1);
  resolve();
  assert.equal(await pending, "accepted");
  assert.equal(controller.state.pending, false);
  controller.dispose();
});

test("a failed browser prompt falls back to instructions and requires a fresh event", async () => {
  const { controller, window } = fixture();
  const event = installEvent(window, "accepted", () => { throw new Error("Browser blocked installation"); });
  assert.equal(await controller.install(), null);
  assert.equal(controller.installed, false);
  assert.equal(controller.state.visible, true);
  assert.equal(controller.state.helpOpen, true);
  assert.match(controller.state.error, /could not open installation/);
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 1);
  const next = installEvent(window);
  assert.equal(await controller.install(), "accepted");
  assert.equal(next.calls, 1);
  controller.dispose();
});

test("an unknown browser result never reports installed or hides the manual fallback", async () => {
  const { controller, window } = fixture();
  const event = installEvent(window, "unknown");
  assert.equal(await controller.install(), null);
  assert.equal(event.calls, 1);
  assert.equal(controller.state.visible, true);
  assert.match(controller.state.error, /not confirmed/);
  assert.equal(controller.installed, false);
  controller.dispose();
});

test("late browser responses cannot update a disposed installation controller", async () => {
  const { controller, window, changes } = fixture();
  let resolve;
  installEvent(window, "accepted", () => new Promise(done => { resolve = done; }));
  const pending = controller.install();
  controller.dispose();
  const count = changes.length;
  resolve();
  assert.equal(await pending, null);
  assert.equal(changes.length, count);
  const event = installEvent(window);
  assert.equal(event.defaultPrevented, false);
  window.dispatchEvent(new Event("appinstalled"));
  assert.equal(controller.installed, false);
});

function rootFixture() {
  const buttons = new Map();
  const root = {
    hidden: true,
    html: "",
    set innerHTML(value) { this.html = value; },
    get innerHTML() { return this.html; },
    querySelector(selector) {
      if (!buttons.has(selector)) buttons.set(selector, {});
      return buttons.get(selector);
    },
    replaceChildren() { this.html = ""; }
  };
  return { root, buttons };
}

test("mounted offer keeps scanning available and connects install and dismiss buttons", async () => {
  const { window, controller: initial, storage } = fixture();
  initial.dispose();
  const { root, buttons } = rootFixture();
  const controller = mountScannerInstall({ root, window, navigator: {}, storage });
  assert.equal(root.hidden, false);
  assert.match(root.innerHTML, /Internet is required to record meals/);
  assert.match(root.innerHTML, /How to install/);
  assert.doesNotMatch(root.innerHTML, /dialog|aria-modal|download=|\.apk/);
  await buttons.get("[data-scanner-install]").onclick();
  assert.match(root.innerHTML, /browser menu/);
  installEvent(window);
  assert.match(root.innerHTML, /Install app/);
  buttons.get("[data-scanner-install-dismiss]").onclick();
  assert.equal(root.hidden, true);
  assert.equal(root.innerHTML, "");
  controller.dispose();
});

test("blocked sessionStorage property and missing mount cannot interrupt scanner startup", () => {
  const { window, controller: initial } = fixture();
  initial.dispose();
  Object.defineProperty(window, "sessionStorage", { get() { throw new Error("Blocked"); } });
  const { root } = rootFixture();
  const controller = mountScannerInstall({ root, window, navigator: {} });
  assert.equal(root.hidden, false);
  assert.doesNotThrow(() => controller.dismiss());
  assert.equal(mountScannerInstall({ root: null, window }), null);
  controller.dispose();
});

test("scanner registers installation before awaiting connection and outside rendering", () => {
  const source = readFileSync(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.ok(source.indexOf('mountScannerInstall({ root: document.getElementById("scanner-install")') < source.indexOf("async function openScanner"));
  assert.equal(source.match(/mountScannerInstall\(\{/g)?.length, 1);
});

test("manual guidance never promises an APK download or offline meal approval", () => {
  for (const options of [{}, { navigator: { userAgent: "iPhone" } }, { secureContext: false }]) {
    assert.doesNotMatch(scannerInstallHelp(options), /APK|offline approval|notification permission/);
  }
});
