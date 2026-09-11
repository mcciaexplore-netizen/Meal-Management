import { escape } from "./core.js";

const dismissalKey = "meal-scanner-install-dismissed-v1";

export function scannerInstallHelp({ navigator = {}, secureContext = true } = {}) {
  if (!secureContext) return "Open the scanner's HTTPS link to install it. You can continue using this page in your browser.";
  const appleMobile = /iPad|iPhone|iPod/.test(navigator.userAgent ?? "") || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (appleMobile) return "Open this link in Safari. Tap Share, then Add to Home Screen, then Add. If shown, keep Open as Web App enabled.";
  return "Open your browser menu and look for Install app or Add to Home screen. On a computer, an install icon may appear in the address bar. If these options are unavailable, use a browser that supports installation, such as Chrome or Edge, or keep scanning here.";
}

export class ScannerInstallController {
  constructor({ window, navigator = {}, storage = null, onChange = () => {} }) {
    this.window = window;
    this.navigator = navigator;
    this.storage = storage;
    this.onChange = onChange;
    this.media = window.matchMedia?.("(display-mode: standalone)");
    this.deferred = null;
    this.pending = false;
    this.installed = false;
    this.disposed = false;
    this.helpOpen = false;
    this.error = "";
    this.dismissed = false;
    try { this.dismissed = storage?.getItem(dismissalKey) === "1"; } catch {}
    this.beforeInstall = event => {
      if (this.disposed || this.standalone || this.window.isSecureContext === false || typeof event.prompt !== "function") return;
      event.preventDefault();
      if (!this.dismissed && !this.pending) {
        this.deferred = event;
        this.error = "";
        this.emit();
      }
    };
    this.appInstalled = () => {
      this.installed = true;
      this.deferred = null;
      this.rememberDismissal();
      this.emit();
    };
    this.displayChanged = () => this.emit();
    window.addEventListener("beforeinstallprompt", this.beforeInstall);
    window.addEventListener("appinstalled", this.appInstalled);
    if (this.media?.addEventListener) this.media.addEventListener("change", this.displayChanged);
    else this.media?.addListener?.(this.displayChanged);
    this.emit();
  }

  get standalone() {
    return this.installed || this.media?.matches === true || this.navigator.standalone === true;
  }

  get state() {
    return {
      visible: !this.disposed && !this.standalone && !this.dismissed,
      canInstall: Boolean(this.deferred) && !this.pending,
      pending: this.pending,
      helpOpen: this.helpOpen,
      help: scannerInstallHelp({ navigator: this.navigator, secureContext: this.window.isSecureContext !== false }),
      error: this.error
    };
  }

  emit() {
    if (!this.disposed) this.onChange(this.state);
  }

  rememberDismissal() {
    this.dismissed = true;
    try { this.storage?.setItem(dismissalKey, "1"); } catch {}
  }

  dismiss() {
    this.rememberDismissal();
    this.deferred = null;
    this.emit();
  }

  showHelp() {
    this.helpOpen = !this.helpOpen;
    this.emit();
  }

  async install() {
    if (this.disposed || this.pending || this.standalone || this.dismissed) return null;
    const event = this.deferred;
    if (!event) { this.helpOpen = true; this.emit(); return null; }
    this.deferred = null;
    this.pending = true;
    this.error = "";
    this.emit();
    try {
      const prompted = await event.prompt();
      const choice = event.userChoice ? await event.userChoice : prompted;
      if (this.disposed) return null;
      if (choice?.outcome === "accepted" || choice?.outcome === "dismissed") {
        this.rememberDismissal();
        return choice.outcome;
      }
      this.error = "Installation was not confirmed. You can keep scanning in this browser.";
      this.helpOpen = true;
      return null;
    } catch {
      if (!this.disposed) {
        this.error = "The browser could not open installation. You can keep scanning here or use the browser menu.";
        this.helpOpen = true;
      }
      return null;
    } finally {
      this.pending = false;
      this.emit();
    }
  }

  dispose() {
    this.disposed = true;
    this.deferred = null;
    this.window.removeEventListener("beforeinstallprompt", this.beforeInstall);
    this.window.removeEventListener("appinstalled", this.appInstalled);
    if (this.media?.removeEventListener) this.media.removeEventListener("change", this.displayChanged);
    else this.media?.removeListener?.(this.displayChanged);
  }
}

export function mountScannerInstall({ root, window, navigator = window.navigator, storage } = {}) {
  if (!root) return null;
  if (storage === undefined) {
    try { storage = window.sessionStorage; } catch { storage = null; }
  }
  let controller;
  const render = state => {
    root.hidden = !state.visible;
    if (!state.visible) { root.replaceChildren(); return; }
    root.innerHTML = `<div class="scanner-install-banner"><div class="scanner-install-copy"><h2 id="scanner-install-title">Install Meal Scanner</h2><p>Add a home-screen icon for quick access. Internet is required to record meals.</p></div><div class="scanner-install-actions"><button type="button" class="button primary" data-scanner-install ${state.pending ? "disabled" : ""}>${state.pending ? "Opening…" : state.canInstall ? "Install app" : "How to install"}</button><button type="button" class="button" data-scanner-install-dismiss aria-label="Dismiss installation reminder">Not now</button></div>${state.helpOpen ? `<p class="scanner-install-help">${escape(state.help)}</p>` : ""}${state.error ? `<p class="scanner-install-error" role="status">${escape(state.error)}</p>` : ""}</div>`;
    root.querySelector("[data-scanner-install]").onclick = () => controller.install();
    root.querySelector("[data-scanner-install-dismiss]").onclick = () => controller.dismiss();
  };
  controller = new ScannerInstallController({ window, navigator, storage, onChange: render });
  return controller;
}
