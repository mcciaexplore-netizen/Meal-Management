import { allPages, api } from "./api.js";
import { escape, isFinalScanResult, ServingOperation, timestamp } from "./core.js";
import { badge, field, heading, icon, notify, options, selectField } from "./ui.js";
import { CameraSession, cameraError } from "./camera.js";
import { decodeImage, decodeSource, nativeQrDetector } from "./qr_decoder.js";
import { approvedReceipt, scanDetails, servingLocations, servingScanners } from "./scan_helpers.js";
import { clearServingMarker, loadServingMarker, saveServingMarker } from "./scan_state.js";

let retained = null;
let scansSuspended = false;
let suspendActive = () => {};
let resumeActive = () => {};

export function suspendScanning() {
  scansSuspended = true;
  suspendActive();
}

export function resumeScanning() {
  scansSuspended = false;
  resumeActive();
}

export function clearScanning() {
  suspendActive();
  retained = null;
  scansSuspended = false;
}

const rejectionMessages = {
  UNKNOWN_QR: "This QR is not recognized.",
  INVALID_QR: "This is not a valid meal QR.",
  QR_NOT_FOUND: "This QR is not recognized.",
  INVALID_QR_TOKEN: "This is not a valid meal QR.",
  QR_REVOKED: "This credential has been revoked.",
  QR_EXPIRED: "This credential has expired.",
  EMPLOYEE_INACTIVE: "This employee is inactive.",
  EMPLOYEE_UNAVAILABLE: "This employee is not eligible for a meal.",
  AUTHORIZATION_REQUIRED: "Select an administrator's visitor approval first.",
  ADMIN_AUTHORIZATION_REQUIRED: "Select an administrator's visitor approval first.",
  AUTHORIZATION_SCOPE_MISMATCH: "The QR, waiter, counter, meal, or quantity does not match the visitor approval.",
  AUTHORIZING_ADMIN_UNAVAILABLE: "The authorizing administrator no longer has active approval access.",
  AUTHORIZATION_REVOKED: "The administrator revoked this visitor approval.",
  AUTHORIZATION_NOT_APPLICABLE: "Employee meals do not use visitor approvals. Start an employee serving.",
  EMPLOYEE_QUANTITY_MUST_BE_ONE: "Each employee serving records one meal. Begin another serving for an additional meal.",
  AUTHORIZATION_EXPIRED: "The visitor approval has expired.",
  AUTHORIZATION_ALREADY_USED: "This visitor approval has already been used.",
  AUTHORIZATION_MISMATCH: "The scan details do not match the visitor approval.",
  IDEMPOTENCY_KEY_REUSED: "This serving identifier belongs to different scan details.",
  REQUEST_MISMATCH: "The serving identifier does not match this request.",
  SCANNER_UNAVAILABLE: "This scanning counter is unavailable.",
  UNKNOWN_SCANNER: "This scanning counter is not registered.",
  SCANNER_INACTIVE: "This scanning counter is inactive.",
  LOCATION_INACTIVE: "This serving location is inactive.",
  SCANNER_LOCATION_CHANGED: "The counter location changed. Ask the administrator to verify the serving details.",
  MEAL_TYPE_UNAVAILABLE: "This meal type is no longer available."
};

export async function scanScreen(target, context) {
  const approvals = (await allPages("/visitor-authorizations?status=pending")).filter(row => Number(row.waiter_id) === Number(context.user.staff_id));
  if (!context.isCurrent()) return;
  let storage = null;
  try { storage = window.sessionStorage; } catch {}
  const marker = loadServingMarker(storage, context.user.staff_id);
  let markerSaved = Boolean(marker);
  const previous = retained?.staffId === context.user.staff_id ? retained : marker ? { locationId: marker.location_id, scannerCode: marker.request.scanner_code, mealTypeId: marker.request.meal_type_id } : null;
  const operation = previous?.operation ?? new ServingOperation();
  if (!operation.request && marker) operation.restore(marker.request);
  const locations = servingLocations(context.catalog);
  const activeMeals = (context.catalog.meal_types ?? []).filter(row => row.is_active === true || row.is_active === 1);
  retained = { ...previous, staffId: context.user.staff_id, operation };
  target.innerHTML = `${heading("SERVING STATION", "Scan. Confirm. Serve.", "Select the serving location, registered counter, and meal type.")}<div class="scan-layout"><section class="panel scanner-panel"><div class="camera-stage" id="camera-stage"><video id="camera-video" playsinline muted aria-label="Live QR camera"></video><div class="camera-placeholder" id="camera-placeholder">${icon("scan")}<h2>Ready to scan</h2><p>Start the camera, or upload a QR image below.</p></div><div class="camera-guide" id="camera-guide" hidden><span></span><span></span><span></span><span></span></div></div><div class="camera-controls" aria-label="Camera controls"><button type="button" class="button primary" id="start-camera">Start camera</button><button type="button" class="button" id="stop-camera" disabled>Stop camera</button><button type="button" class="button" id="switch-camera" disabled>Switch camera</button></div><div class="camera-caption"><span class="status-dot"></span><span id="camera-state" role="status">Camera is off</span><span class="camera-private">Images stay on this device</span></div><div id="camera-error" class="camera-message" role="alert" hidden></div><div id="scan-result" class="scan-result" aria-live="assertive" aria-atomic="true"><div class="scan-idle">${icon("scan")}<div><strong>Waiting for a QR</strong><p>Each approved serving is saved before you see confirmation.</p></div></div></div><div class="scan-actions"><button type="button" class="button primary" id="retry-serving" hidden>Retry this serving</button><button type="button" class="button primary" id="next-serving" disabled>Scan next meal ${icon("arrow")}</button></div></section><section class="panel padded"><p class="eyebrow">CURRENT SERVING</p><h2>Service details</h2><form id="scan-details">${selectField("Serving location", "location_id", locations, previous?.locationId ?? "")}${selectField("Registered scanning counter", "scanner_code", servingScanners(context.catalog, previous?.locationId), previous?.scannerCode ?? "", { value: "code" })}<p class="field-help" id="scanner-location">Choose an active counter at this location.</p>${selectField("Meal type", "meal_type_id", activeMeals, previous?.mealTypeId ?? "")}<label>Serving category<select name="category"><option value="EMPLOYEE">Employee meal</option><option value="MASTER">Authorized visitor meals</option></select></label><div id="visitor-fields" hidden><label>Administrator approval<select name="authorization_id"><option value="">Select an assigned visitor approval</option>${approvals.map(row => `<option value="${escape(row.authorization_id ?? row.id)}">${escape(row.visitor_name)} · ${escape(row.quantity)} meal${row.quantity === 1 ? "" : "s"}</option>`).join("")}</select></label><div id="approval-detail" class="approval-detail"></div></div><div class="detail-section"><h3>Upload a QR image</h3><p class="muted small">Use a saved QR or photo if camera access is unavailable.</p><label>QR image<input type="file" name="qr_image" accept="image/png,image/jpeg,image/webp,image/svg+xml"><span class="field-help">PNG, JPG, WebP, or SVG, up to 10 MB. Decoded privately on this device.</span></label><button type="button" class="button full" id="submit-image">Scan uploaded image</button></div><details class="detail-section"><summary>Manual credential entry</summary><p class="muted small">Use the credential text only when other scanning methods are unavailable.</p>${field("QR credential", "token", "", "password", 'autocomplete="off" spellcheck="false" maxlength="256"')}<button type="button" class="button full" id="submit-manual">Record serving</button></details></form><p class="field-help">Wait for approval before handing over meals. Select <strong>Scan next meal</strong> for a deliberate new serving.</p></section></div>`;
  const video = target.querySelector("#camera-video");
  const form = target.querySelector("#scan-details");
  const resultPanel = target.querySelector("#scan-result");
  const nextButton = target.querySelector("#next-serving");
  const retryButton = target.querySelector("#retry-serving");
  const startButton = target.querySelector("#start-camera");
  const stopButton = target.querySelector("#stop-camera");
  const switchButton = target.querySelector("#switch-camera");
  const canvas = document.createElement("canvas");
  let disposed = false;
  let suspended = scansSuspended;
  let decodingImage = false;
  let recovering = Boolean(operation.request && !operation.result);
  let decodeEpoch = 0;
  let receiptEpoch = 0;
  let timer = null;
  let detector = null;
  let camera;

  function updateControls(state = camera?.state ?? "stopped") {
    const blocked = Boolean(operation.result || operation.request?.token) || operation.inFlight || recovering || decodingImage || suspended || disposed;
    startButton.disabled = blocked || state !== "stopped";
    stopButton.disabled = state === "stopped";
    switchButton.disabled = blocked || state !== "active";
    target.querySelector("#camera-placeholder").hidden = state === "active";
    target.querySelector("#camera-guide").hidden = state !== "active";
    target.querySelector("#camera-stage").classList.toggle("camera-on", state === "active");
    target.querySelector("#camera-state").textContent = state === "active" ? "Camera active · scanning one QR" : state === "starting" ? "Starting camera…" : "Camera is off";
  }

  camera = new CameraSession({ video, mediaDevices: navigator.mediaDevices, secureContext: window.isSecureContext, onState: updateControls });

  function stopCamera() {
    decodeEpoch += 1;
    clearTimeout(timer);
    camera.stop();
  }

  function lockDetails() {
    const locked = Boolean(operation.request) || decodingImage || suspended || recovering;
    [...form.elements].forEach(element => { element.disabled = locked; });
    if (operation.request && !operation.request.token && !operation.result && !operation.inFlight && !decodingImage && !suspended && !recovering) {
      [form.elements.token, form.elements.qr_image, target.querySelector("#submit-image"), target.querySelector("#submit-manual")].forEach(element => { element.disabled = false; });
    }
    if (!locked && form.elements.category.value === "MASTER" && form.elements.authorization_id.value) {
      form.elements.location_id.disabled = true;
      form.elements.scanner_code.disabled = true;
      form.elements.meal_type_id.disabled = true;
    }
    updateControls();
  }

  function reportCameraError(error) {
    const area = target.querySelector("#camera-error");
    area.textContent = cameraError(error);
    area.hidden = false;
  }

  function clearCameraError() {
    target.querySelector("#camera-error").hidden = true;
  }

  function details(token) {
    if (operation.request) {
      if (typeof token !== "string" || !token || token.trim() !== token || token.length > 256) throw new Error("Provide the original QR credential to retry this serving.");
      return { ...operation.request, token: operation.request.token || token };
    }
    return scanDetails({ token, location_id: form.elements.location_id.value, scanner_code: form.elements.scanner_code.value, meal_type_id: form.elements.meal_type_id.value, category: form.elements.category.value, authorization_id: form.elements.authorization_id.value }, context.catalog, approvals, context.user.staff_id);
  }

  function updateCounters(selected = "") {
    const scanners = servingScanners(context.catalog, form.elements.location_id.value);
    form.elements.scanner_code.innerHTML = `<option value="">Select registered counter</option>${options(scanners, selected, "name", "code")}`;
    target.querySelector("#scanner-location").textContent = scanners.length ? "This counter determines the recorded serving location." : "No active registered counter at this location. Contact the administrator.";
  }

  async function showReceipt(result) {
    const epoch = ++receiptEpoch;
    const section = target.querySelector("#scan-receipt");
    if (!section) return;
    section.innerHTML = '<p class="muted small">Loading serving details…</p>';
    const loaded = await approvedReceipt(result, requestId => api(`/scans/${encodeURIComponent(requestId)}/receipt`));
    if (disposed || !context.isCurrent() || epoch !== receiptEpoch || !section.isConnected) return;
    if (!loaded.receipt) {
      section.innerHTML = '<p class="muted small">The meal is recorded. Identity details are temporarily unavailable.</p><button type="button" class="small-button" id="retry-receipt">Retry details</button>';
      section.querySelector("#retry-receipt").onclick = () => showReceipt(result);
      return;
    }
    const receipt = loaded.receipt;
    const name = receipt.kind === "EMPLOYEE" ? receipt.employee_name : receipt.visitor_name;
    section.innerHTML = `<div class="receipt-identity">${receipt.kind === "EMPLOYEE" && receipt.photo_available ? `<img class="receipt-photo" src="/api/scans/${encodeURIComponent(result.request_id)}/photo" alt="Employee photo for ${escape(name)}">` : `<span class="large-avatar">${icon(receipt.kind === "EMPLOYEE" ? "people" : "qr")}</span>`}<div><strong>${escape(name ?? "Recorded serving")}</strong><span>${receipt.kind === "EMPLOYEE" ? "Employee meal" : "Authorized visitor meals"}</span><span>${escape(receipt.quantity)} meal${Number(receipt.quantity) === 1 ? "" : "s"} · ${escape(timestamp(receipt.served_at))}</span></div></div><p class="field-help" id="photo-status">${receipt.kind === "EMPLOYEE" && !receipt.photo_available ? "No employee photo is available for this serving." : ""}</p>`;
    section.querySelector(".receipt-photo")?.addEventListener("error", event => {
      event.currentTarget.hidden = true;
      section.querySelector("#photo-status").textContent = "Photo unavailable. The approved meal remains recorded.";
    });
  }

  function showResult(result) {
    stopCamera();
    retryButton.hidden = true;
    nextButton.disabled = suspended;
    const title = result.approved ? result.duplicate ? "Already recorded" : "Meal approved" : "Meal not approved";
    const count = result.meal_ids?.length ?? operation.request?.quantity ?? 1;
    const description = result.approved ? result.duplicate ? "This serving was already saved. Do not serve it a second time." : `${count} meal${count === 1 ? "" : "s"} recorded. You may serve now.` : rejectionMessages[result.code] ?? "This attempt did not approve a meal. Check the serving details or contact the administrator.";
    resultPanel.innerHTML = `<div class="decision ${result.approved ? result.duplicate ? "duplicate" : "approved" : "rejected"}"><span class="decision-icon">${icon(result.approved ? "check" : "close")}</span><div><p class="eyebrow">${result.approved ? "COMMITTED RESULT" : "SCAN REJECTED"}</p><h2>${title}</h2><p>${escape(description)}</p>${result.serving_id ? `<span class="decision-reference">Serving #${escape(result.serving_id)}</span>` : badge(result.code, "danger")}</div></div>${result.approved ? '<div class="scan-receipt" id="scan-receipt" aria-live="polite"></div>' : ""}`;
    lockDetails();
    if (result.approved) showReceipt(result);
  }

  function showUnconfirmed(message = "The earlier serving has not been confirmed.") {
    const needsToken = !operation.request?.token;
    resultPanel.innerHTML = `<div class="decision uncertain"><span class="decision-icon">${icon("scan")}</span><div><h2>Network / result not confirmed</h2><p>${escape(message)}</p><p>${needsToken ? "Check the previous result, or scan the original QR again. The same serving identifier and details will be retained." : "Do not start another meal. Retry this same serving to retrieve its committed result."}</p></div></div>`;
    retryButton.textContent = needsToken ? "Check previous result" : "Retry this serving";
    retryButton.hidden = false;
    retryButton.disabled = suspended || recovering;
    nextButton.disabled = true;
    lockDetails();
  }

  async function recoverResult() {
    if (!operation.request || operation.result || recovering && retryButton.disabled) return;
    recovering = true;
    retryButton.disabled = true;
    lockDetails();
    try {
      const result = await api(`/scans/${encodeURIComponent(operation.request.request_id)}/result`);
      if (disposed || !context.isCurrent()) return;
      recovering = false;
      if (operation.result) { showResult({ ...operation.result, duplicate: operation.result.approved }); return; }
      if (!operation.accept(result)) showUnconfirmed("The original serving is still awaiting a confirmed result.");
      else {
        showResult({ ...result, duplicate: result.approved });
      }
    } catch (error) {
      if (disposed || !context.isCurrent()) return;
      recovering = false;
      if (operation.result) { showResult({ ...operation.result, duplicate: operation.result.approved }); return; }
      showUnconfirmed(error.status === 404 ? "The original request has not been found. Rescan its original QR to submit the same serving safely." : error.message);
    } finally {
      if (!disposed) { recovering = false; lockDetails(); }
    }
  }

  async function submit(token) {
    if (operation.inFlight || operation.result || disposed || suspended || recovering) return;
    let input;
    try { input = details(token); }
    catch (error) { notify(error.message, "error"); stopCamera(); return; }
    retained.scannerCode = input.scanner_code;
    retained.mealTypeId = input.meal_type_id;
    retained.locationId = operation.request ? retained.locationId : form.elements.location_id.value;
    stopCamera();
    operation.bind(input);
    const saved = saveServingMarker(storage, context.user.staff_id, operation.request, retained.locationId);
    markerSaved = markerSaved || saved;
    if (!saved) notify("This browser cannot retain the serving across a reload. Keep this page open until its result is confirmed.", "error");
    lockDetails();
    retryButton.hidden = true;
    nextButton.disabled = true;
    resultPanel.innerHTML = '<div class="scan-pending" role="status"><span class="spinner"></span><strong>Recording this serving…</strong><p>Wait for the committed result before serving.</p></div>';
    try {
      const response = await operation.submit(body => api("/scans", { method: "POST", body }), input);
      if (!disposed && response) {
        if (isFinalScanResult(response, operation.request)) showResult(response);
        else {
          if (["IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH"].includes(response.code)) operation.forgetCredential();
          showUnconfirmed(response.code === "IDEMPOTENCY_KEY_REUSED" ? "This QR does not match the original serving. Checking the original result before you continue." : "The original serving is still awaiting a confirmed result.");
          recoverResult();
        }
      }
    } catch (error) {
      if (disposed) return;
      if (["IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH"].includes(error.code)) operation.forgetCredential();
      showUnconfirmed(error.message);
    }
  }

  async function decode(epoch) {
    if (disposed || suspended || epoch !== decodeEpoch || camera.state !== "active" || operation.result || operation.request?.token || recovering) return;
    if (video.readyState >= 2 && video.videoWidth) {
      let token = null;
      try { token = await decodeSource(video, canvas, detector, 900); }
      catch (error) { stopCamera(); reportCameraError(error); return; }
      if (token && epoch === decodeEpoch && !disposed && !suspended) { await submit(token); return; }
    }
    if (epoch === decodeEpoch && !disposed && !suspended) timer = setTimeout(() => decode(epoch), 180);
  }

  async function startCamera(switching = false) {
    if (operation.result || operation.request?.token || operation.inFlight || disposed || suspended || decodingImage || recovering) return;
    try {
      details("camera-preflight");
      clearCameraError();
      decodeEpoch += 1;
      clearTimeout(timer);
      const epoch = decodeEpoch;
      const started = switching ? await camera.switch() : await camera.start();
      if (!started || epoch !== decodeEpoch || disposed || suspended) return;
      detector = await nativeQrDetector();
      if (epoch === decodeEpoch && !disposed && !suspended) await decode(epoch);
    } catch (error) {
      stopCamera();
      reportCameraError(error);
    }
  }

  async function uploadImage() {
    if (operation.result || operation.request?.token || operation.inFlight || disposed || suspended || decodingImage || recovering) return;
    try {
      details("image-preflight");
      stopCamera();
      clearCameraError();
      decodingImage = true;
      const epoch = decodeEpoch;
      lockDetails();
      target.querySelector("#submit-image").textContent = "Reading QR image…";
      const token = await decodeImage(form.elements.qr_image.files[0], canvas, await nativeQrDetector());
      if (disposed || suspended || epoch !== decodeEpoch) return;
      await submit(token);
    } catch (error) {
      if (!disposed && !suspended) reportCameraError(error);
    } finally {
      decodingImage = false;
      if (!disposed) {
        form.elements.qr_image.value = "";
        target.querySelector("#submit-image").textContent = "Scan uploaded image";
        lockDetails();
      }
    }
  }

  function selectApproval() {
    stopCamera();
    const approval = approvals.find(row => Number(row.authorization_id ?? row.id) === Number(form.elements.authorization_id.value));
    target.querySelector("#approval-detail").innerHTML = "";
    if (approval) {
      form.elements.location_id.value = String(approval.location_id);
      updateCounters(approval.scanner_code);
      form.elements.meal_type_id.value = String(approval.meal_type_id);
      target.querySelector("#approval-detail").innerHTML = `<strong>${escape(approval.visitor_name)}</strong><p>${escape(approval.quantity)} meals · ${escape(approval.visitor_organization ?? "Visitor party")}</p><span class="small">Expires ${escape(timestamp(approval.expires_at))}</span>`;
    }
    lockDetails();
  }

  form.addEventListener("submit", event => { event.preventDefault(); submit(form.elements.token.value); });
  form.elements.location_id.addEventListener("change", () => { stopCamera(); updateCounters(); });
  form.elements.scanner_code.addEventListener("change", stopCamera);
  form.elements.meal_type_id.addEventListener("change", stopCamera);
  form.elements.category.addEventListener("change", () => {
    stopCamera();
    target.querySelector("#visitor-fields").hidden = form.elements.category.value !== "MASTER";
    if (form.elements.category.value === "EMPLOYEE") form.elements.authorization_id.value = "";
    lockDetails();
  });
  form.elements.authorization_id.addEventListener("change", selectApproval);
  startButton.onclick = () => startCamera();
  stopButton.onclick = stopCamera;
  switchButton.onclick = () => startCamera(true);
  target.querySelector("#submit-image").onclick = uploadImage;
  target.querySelector("#submit-manual").onclick = () => submit(form.elements.token.value);
  retryButton.onclick = () => {
    if (operation.result) { showResult({ ...operation.result, duplicate: operation.result.approved }); return; }
    if (operation.request?.token) submit(operation.request.token);
    else recoverResult();
  };
  nextButton.onclick = async () => {
    if (!operation.result || suspended) return;
    receiptEpoch += 1;
    if (markerSaved && !clearServingMarker(storage, context.user.staff_id)) { notify("The previous serving marker could not be cleared. Reload before starting another meal.", "error"); return; }
    operation.reset();
    await context.reload();
  };
  if (operation.request) {
    const originalScanner = context.catalog.scanners.find(row => row.code === operation.request.scanner_code);
    form.elements.location_id.value = String(previous?.locationId ?? originalScanner?.location_id ?? "");
    updateCounters(operation.request.scanner_code);
    form.elements.meal_type_id.value = String(operation.request.meal_type_id);
    form.elements.category.value = operation.request.authorization_id ? "MASTER" : "EMPLOYEE";
    target.querySelector("#visitor-fields").hidden = !operation.request.authorization_id;
    if (operation.request.authorization_id) form.elements.authorization_id.value = String(operation.request.authorization_id);
    if (operation.result) showResult({ ...operation.result, duplicate: operation.result.approved });
    else {
      recovering = false;
      showUnconfirmed();
      recoverResult();
    }
  }
  lockDetails();
  function suspend() {
    suspended = true;
    stopCamera();
    lockDetails();
    retryButton.disabled = true;
    nextButton.disabled = true;
  }
  function resume() {
    if (disposed) return;
    suspended = false;
    lockDetails();
    retryButton.disabled = false;
    nextButton.disabled = !operation.result;
  }
  suspendActive = suspend;
  resumeActive = resume;
  const beforeUnload = event => {
    if (operation.request && !operation.result) { event.preventDefault(); event.returnValue = ""; }
  };
  const hide = () => { if (document.hidden) stopCamera(); };
  window.addEventListener("beforeunload", beforeUnload);
  window.addEventListener("pagehide", stopCamera);
  document.addEventListener("visibilitychange", hide);
  return () => {
    disposed = true;
    receiptEpoch += 1;
    stopCamera();
    camera.dispose();
    if (suspendActive === suspend) suspendActive = () => {};
    if (resumeActive === resume) resumeActive = () => {};
    window.removeEventListener("beforeunload", beforeUnload);
    window.removeEventListener("pagehide", stopCamera);
    document.removeEventListener("visibilitychange", hide);
    form.elements.token.value = "";
    form.elements.qr_image.value = "";
  };
}
