import { escape } from "./core.js";
import { errorMessage, field, icon } from "./ui.js";
import { CameraSession, cameraError } from "./camera.js";
import { decodeImage, decodeSource, nativeQrDetector } from "./qr_decoder.js";
import { clearScannerMarker, loadActiveScannerMarker, loadScannerMarker, saveScannerMarker } from "./scanner_state.js";
import { ScannerTransport } from "./scanner_api.js";
import { ScanAppOperation } from "./scan_app_operation.js";
import { chooseScannerRecovery } from "./scanner_recovery.js";

const root = document.getElementById("scan-app");
const transport = new ScannerTransport();
let flow = null;
let servingScope = null;
let dispose = () => {};
let version = 0;
let markerSaved = false;
let storage = null;
try { storage = window.sessionStorage; } catch {}

export function scanOutcomeMessage(code) {
  const messages = {
    QR_EXPIRED: "QR has expired. Please contact the administrator.",
    QR_REVOKED: "QR has been revoked. Please contact the administrator.",
    EMPLOYEE_INACTIVE: "Employee is inactive. Please contact the administrator.",
    UNKNOWN_QR: "QR is not recognized. Please contact the administrator.",
    INVALID_QR: "QR is not recognized. Please contact the administrator."
  };
  return messages[code] ?? String(code).replaceAll("_", " ").toLowerCase();
}

async function openScanner() {
  const current = ++version;
  dispose();
  root.innerHTML = '<main id="main" class="startup"><div class="spinner"></div><h1>Opening scanner…</h1></main>';
  try {
    const session = await transport.connect();
    if (current !== version) return;
    const activeMarker = loadActiveScannerMarker(storage, session.scope);
    if (activeMarker && activeMarker.scope !== session.scope) throw new Error("The previous serving belongs to another browser session. Ask the office administrator to confirm it before serving another meal.");
    if (flow?.request && servingScope !== session.scope) throw new Error("The previous serving belongs to another browser session. Ask the office administrator to confirm it before serving another meal.");
    servingScope = session.scope;
    const localMarker = activeMarker?.request ?? loadScannerMarker(storage, servingScope);
    const recovery = await chooseScannerRecovery({
      localMarker,
      currentRequest: flow?.request,
      recover: () => transport.request("/recovery", {}, session.scope)
    });
    if (current !== version) return;
    markerSaved = Boolean(localMarker);
    if (recovery.fromServer) markerSaved = saveScannerMarker(storage, servingScope, recovery.request);
    if (!flow) flow = new ScanAppOperation();
    if (!flow.request && recovery.request) flow.restore(recovery.request);
    renderScanner();
  } catch (error) {
    if (current !== version) return;
    if (error.code === "SCANNER_ACTIVATION_REQUIRED") {
      renderActivation();
      return;
    }
    root.innerHTML = `<main id="main" class="startup"><h1>Scanner unavailable</h1>${errorMessage(error)}<button class="button primary" id="retry-startup">Try again</button></main>`;
    root.querySelector("#retry-startup").onclick = openScanner;
  }
}

function renderActivation() {
  const current = ++version;
  dispose();
  root.innerHTML = `<main id="main" class="startup scanner-activation"><div class="scan-app-brand">${icon("scan")}<strong>Meal Scanner</strong></div><h1>Activate this scanner</h1><p>Enter the office scanner activation code once. This device can then scan securely from any internet connection.</p><form id="scanner-activation-form">${field("Activation code", "activation_code", "", "password", 'required minlength="12" maxlength="256" autocomplete="one-time-code" spellcheck="false"')}<div data-form-error></div><button class="button primary full" type="submit">Activate scanner</button></form></main>`;
  const form = root.querySelector("#scanner-activation-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector("button");
    const target = form.querySelector("[data-form-error]");
    target.innerHTML = "";
    button.disabled = true;
    button.textContent = "Activating…";
    try {
      await transport.activate(new FormData(form).get("activation_code"));
      if (current === version) await openScanner();
    } catch (error) {
      if (current === version) target.innerHTML = errorMessage(error);
    } finally {
      if (current === version) {
        button.disabled = false;
        button.textContent = "Activate scanner";
      }
    }
  });
}

function renderScanner() {
  dispose();
  const current = ++version;
  const active = () => current === version;
  root.innerHTML = `<div class="scan-app-shell"><header class="scan-app-header"><div class="scan-app-brand">${icon("scan")}<strong>Meal Scanner</strong></div></header><main id="main" class="scan-only-main"><section class="scan-view" id="capture-view"><div class="scan-only-camera"><video id="scan-video" playsinline muted aria-label="Live QR camera"></video><div class="scan-camera-placeholder" id="scan-camera-placeholder">${icon("scan")}<h1>Scan a meal QR</h1><p>Hold the QR inside the frame.</p></div><div class="scan-camera-frame" id="scan-camera-frame" hidden></div><span class="scan-live-label" id="camera-state">Camera off</span></div><div class="scan-only-controls"><button class="button primary" id="camera-start">Start camera</button><button class="button" id="camera-stop" disabled>Stop</button><button class="button" id="camera-switch" disabled>Switch</button></div><div class="scan-upload"><label class="button" for="qr-upload">Upload QR image</label><input id="qr-upload" type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml"><p>PNG, JPG, WebP, or SVG · images stay on this device</p></div></section><section id="scanner-outcome" class="scan-only-outcome" aria-live="assertive" aria-atomic="true"></section><div id="scanner-message" class="scan-only-message" role="alert" hidden></div><div class="scan-bottom-actions"><button class="button" id="check-serving" hidden>Check previous result</button></div></main></div>`;
  const video = root.querySelector("#scan-video");
  const canvas = document.createElement("canvas");
  const cameraStart = root.querySelector("#camera-start");
  const cameraStop = root.querySelector("#camera-stop");
  const cameraSwitch = root.querySelector("#camera-switch");
  const upload = root.querySelector("#qr-upload");
  const capture = root.querySelector("#capture-view");
  const outcome = root.querySelector("#scanner-outcome");
  const checkServing = root.querySelector("#check-serving");
  const message = root.querySelector("#scanner-message");
  let busy = false;
  let decoding = false;
  let timer = null;
  let resultTimer = null;
  let epoch = 0;
  let detector = null;
  let camera;

  function canAcquire() {
    return active() && !busy && !decoding && !flow.result && !flow.inFlight && !flow.request?.token;
  }

  function controls(state = camera?.state ?? "stopped") {
    if (!active()) return;
    const allowed = canAcquire();
    cameraStart.disabled = !allowed || state !== "stopped";
    cameraStop.disabled = state === "stopped";
    cameraSwitch.disabled = !allowed || state !== "active";
    upload.disabled = !allowed;
    root.querySelector("#scan-camera-placeholder").hidden = state === "active";
    root.querySelector("#scan-camera-frame").hidden = state !== "active";
    root.querySelector("#camera-state").textContent = state === "active" ? "Scanning" : state === "starting" ? "Starting camera…" : "Camera off";
    checkServing.disabled = busy;
  }

  camera = new CameraSession({ video, mediaDevices: navigator.mediaDevices, secureContext: window.isSecureContext, onState: controls });
  function stop() { epoch += 1; clearTimeout(timer); camera.stop(); }
  function showMessage(text) { message.textContent = text; message.hidden = false; }
  function clearMessage() { message.hidden = true; }

  function persist() {
    const saved = saveScannerMarker(storage, servingScope, flow.request);
    markerSaved = markerSaved || saved;
    if (!saved) showMessage("Keep this page open until the result is confirmed. This browser cannot retain the current serving after a reload.");
  }

  const sendRead = body => transport.request("/read", { method: "POST", body }, servingScope);

  function finished(result, recovered = false) {
    stop();
    busy = false;
    capture.hidden = true;
    checkServing.hidden = true;
    clearMessage();
    outcome.innerHTML = result.approved ? `<div class="scan-final accepted">${icon("check")}<h1>Accepted · 1 meal</h1>${result.duplicate || recovered ? '<p>Already recorded. Do not serve again.</p>' : ""}<span>Ready for the next scan in 2 seconds</span></div>` : `<div class="scan-final rejected">${icon("close")}<h1>Meal rejected</h1><p>${escape(scanOutcomeMessage(result.code))}</p><span>Ready for the next scan in 2 seconds</span></div>`;
    controls();
    resultTimer = setTimeout(() => {
      if (!active() || !flow.result) return;
      clearScannerMarker(storage, servingScope);
      flow.reset();
      markerSaved = false;
      renderScanner();
    }, 2000);
  }

  function unconfirmed(text = "The previous serving result is not confirmed.") {
    busy = false;
    capture.hidden = Boolean(flow.request?.token);
    checkServing.hidden = false;
    checkServing.textContent = flow.request?.token ? "Retry same serving" : "Check previous result";
    outcome.innerHTML = `<div class="scan-uncertain"><h2>Result not confirmed</h2><p>${escape(text)}</p><p>${flow.request?.token ? "Retry this serving before serving another meal." : "Check the result, or scan the original QR again."}</p></div>`;
    controls();
  }

  function receive(result, restored = false) {
    if (!active() || !result) return;
    busy = false;
    if (flow.accept(result)) finished(flow.result, restored);
    else {
      if (["IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH"].includes(result.code)) flow.forgetCredential();
      unconfirmed("The original serving must be confirmed before you continue.");
      recover();
    }
  }

  function failedRequest(error) {
    if (["IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH"].includes(error.code)) {
      flow.forgetCredential();
      unconfirmed("The original serving must be confirmed before you continue.");
      recover();
      return;
    }
    unconfirmed(error.message);
  }

  async function recover() {
    if (!flow.request || busy) return;
    if (flow.result) { finished(flow.result, true); return; }
    busy = true;
    outcome.innerHTML = '<div class="scan-working" role="status"><span class="spinner"></span><h2>Confirming serving…</h2></div>';
    controls();
    try {
      const result = await transport.request(`/requests/${encodeURIComponent(flow.request.request_id)}/result`, {}, servingScope);
      if (!active()) return;
      busy = false;
      if (flow.result) finished(flow.result, true);
      else if (flow.accept(result)) finished(result, true);
      else unconfirmed("The original serving is still awaiting a confirmed result.");
    } catch (error) {
      if (active()) unconfirmed(error.status === 404 ? "The request has not been found. Rescan its original QR to confirm the same serving." : error.message);
    } finally { if (active()) { busy = false; controls(); } }
  }

  async function read(token) {
    if (!active() || busy || flow.inFlight || flow.result) return;
    const restored = Boolean(flow.request && !flow.request.token);
    try {
      flow.bind(token);
      persist();
      stop();
      busy = true;
      clearMessage();
      outcome.innerHTML = '<div class="scan-working"><span class="spinner"></span><h2>Checking QR…</h2></div>';
      controls();
      const result = await flow.read(token, sendRead);
      if (active()) receive(result, restored);
    } catch (error) {
      if (!active()) return;
      if (flow.request) failedRequest(error);
      else { busy = false; showMessage(error.message); controls(); }
    }
  }

  async function decode(captureEpoch) {
    if (!canAcquire() || camera.state !== "active" || captureEpoch !== epoch) return;
    try {
      if (video.readyState >= 2 && video.videoWidth) {
        const token = await decodeSource(video, canvas, detector, 900);
        if (token && captureEpoch === epoch && canAcquire()) { await read(token); return; }
      }
      if (captureEpoch === epoch && canAcquire()) timer = setTimeout(() => decode(captureEpoch), 180);
    } catch (error) { stop(); showMessage(cameraError(error)); }
  }

  async function start(switching = false) {
    if (!canAcquire()) return;
    try {
      clearMessage();
      epoch += 1;
      clearTimeout(timer);
      const captureEpoch = epoch;
      const started = switching ? await camera.switch() : await camera.start();
      if (!started || !active() || captureEpoch !== epoch) return;
      detector = await nativeQrDetector();
      if (captureEpoch === epoch && canAcquire()) decode(captureEpoch);
    } catch (error) { stop(); showMessage(cameraError(error)); }
  }

  async function uploadImage() {
    if (!canAcquire()) return;
    stop();
    const captureEpoch = epoch;
    decoding = true;
    controls();
    clearMessage();
    try {
      const token = await decodeImage(upload.files[0], canvas, await nativeQrDetector());
      decoding = false;
      if (active() && captureEpoch === epoch) await read(token);
    } catch (error) { if (active()) showMessage(cameraError(error)); }
    finally { decoding = false; upload.value = ""; if (active()) controls(); }
  }

  cameraStart.onclick = () => start();
  cameraStop.onclick = stop;
  cameraSwitch.onclick = () => start(true);
  upload.onchange = uploadImage;
  checkServing.onclick = async () => {
    if (flow.result) { finished(flow.result, true); return; }
    if (!flow.request?.token) { await recover(); return; }
    if (busy) return;
    busy = true;
    outcome.innerHTML = '<div class="scan-working" role="status"><span class="spinner"></span><h2>Confirming serving…</h2></div>';
    controls();
    try { receive(await flow.retry(sendRead)); }
    catch (error) { if (active()) failedRequest(error); }
  };
  const beforeUnload = event => { if (flow.request && !flow.result) { event.preventDefault(); event.returnValue = ""; } };
  const hide = () => { if (document.hidden) stop(); };
  window.addEventListener("pagehide", stop);
  window.addEventListener("beforeunload", beforeUnload);
  document.addEventListener("visibilitychange", hide);
  dispose = () => {
    stop();
    clearTimeout(resultTimer);
    camera.dispose();
    upload.value = "";
    window.removeEventListener("pagehide", stop);
    window.removeEventListener("beforeunload", beforeUnload);
    document.removeEventListener("visibilitychange", hide);
  };
  controls();
  if (flow.result) finished(flow.result, true);
  else if (flow.request) { unconfirmed(); recover(); }
}


await openScanner();
