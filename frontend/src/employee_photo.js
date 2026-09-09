import { CameraSession, cameraError } from "./camera.js";

export function validateEmployeePhoto(photo, required = false) {
  if (!photo?.size) {
    if (required) throw new Error("The photo was empty. Take another photo or choose a JPG or PNG file.");
    return;
  }
  if (!["image/jpeg", "image/png"].includes(photo.type)) throw new Error("Choose a JPG or PNG photo.");
  if (photo.size > 5 * 1024 * 1024) throw new Error("The photo must be 5 MB or smaller.");
}

export function photoCameraError(error) {
  return cameraError(error).replaceAll("QR image", "photo").replace("Live camera scanning", "Taking a photo");
}

export async function captureEmployeePhoto(video, canvas) {
  if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) throw new Error("The camera is still starting. Wait until your preview appears, then take the photo.");
  const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
  canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
  const context = canvas.getContext("2d");
  if (!context || typeof canvas.toBlob !== "function") throw new Error("This browser cannot capture a photo. Upload a JPG or PNG instead.");
  try { context.drawImage(video, 0, 0, canvas.width, canvas.height); }
  catch { throw new Error("The photo could not be captured. Try again or upload a JPG or PNG."); }
  let photo;
  try { photo = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.9)); }
  catch { throw new Error("The photo could not be captured. Try again or upload a JPG or PNG."); }
  validateEmployeePhoto(photo, true);
  if (photo.type !== "image/jpeg") throw new Error("The camera could not create a JPEG photo. Try again or upload a JPG or PNG.");
  return photo;
}

export class EmployeePhotoCapture {
  constructor({ video, canvas, mediaDevices, secureContext, urls = URL, onChange = () => {} }) {
    this.video = video;
    this.canvas = canvas;
    this.urls = urls;
    this.onChange = onChange;
    this.photo = null;
    this.photoUrl = null;
    this.candidate = null;
    this.candidateUrl = null;
    this.mode = "idle";
    this.epoch = 0;
    this.disposed = false;
    this.camera = new CameraSession({ video, mediaDevices, secureContext, facingMode: "user", onState: () => this.changed() });
  }

  changed() { if (!this.disposed) this.onChange(this); }

  releaseCandidate() {
    if (this.candidateUrl) this.urls.revokeObjectURL(this.candidateUrl);
    this.candidate = null;
    this.candidateUrl = null;
  }

  suspend() {
    this.epoch += 1;
    this.camera.stop();
    if (this.mode === "camera" || this.mode === "capturing") this.mode = "idle";
    this.changed();
  }

  select(photo) {
    if (this.disposed) return false;
    validateEmployeePhoto(photo, true);
    const url = this.urls.createObjectURL(photo);
    this.cancel();
    if (this.photoUrl) this.urls.revokeObjectURL(this.photoUrl);
    this.photo = photo;
    this.photoUrl = url;
    this.changed();
    return true;
  }

  async start() {
    if (this.disposed) return false;
    this.suspend();
    this.releaseCandidate();
    this.mode = "camera";
    const epoch = this.epoch;
    this.changed();
    try {
      const started = await this.camera.start();
      if (this.disposed || this.epoch !== epoch) return false;
      if (!started) this.mode = "idle";
      this.changed();
      return started;
    } catch (error) {
      if (this.disposed || this.epoch !== epoch) return false;
      this.mode = "idle";
      this.changed();
      throw error;
    }
  }

  async capture() {
    if (this.disposed || this.mode !== "camera" || this.camera.state !== "active") return false;
    const epoch = this.epoch;
    this.mode = "capturing";
    this.changed();
    try {
      const photo = await captureEmployeePhoto(this.video, this.canvas);
      if (this.disposed || this.epoch !== epoch) return false;
      this.camera.stop();
      this.releaseCandidate();
      this.candidateUrl = this.urls.createObjectURL(photo);
      this.candidate = photo;
      this.mode = "preview";
      this.changed();
      return true;
    } catch (error) {
      if (this.disposed || this.epoch !== epoch) return false;
      this.suspend();
      throw error;
    }
  }

  usePhoto() {
    if (this.disposed || !this.candidate) return false;
    this.suspend();
    if (this.photoUrl) this.urls.revokeObjectURL(this.photoUrl);
    this.photo = this.candidate;
    this.photoUrl = this.candidateUrl;
    this.candidate = null;
    this.candidateUrl = null;
    this.mode = "idle";
    this.changed();
    return true;
  }

  cancel() {
    this.suspend();
    this.releaseCandidate();
    this.mode = "idle";
    this.changed();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.epoch += 1;
    this.camera.dispose();
    this.releaseCandidate();
    if (this.photoUrl) this.urls.revokeObjectURL(this.photoUrl);
    this.photo = null;
    this.photoUrl = null;
  }
}

export function employeePhotoField() {
  return `<section class="registration-photo" aria-labelledby="registration-photo-label"><div class="registration-photo-header"><span id="registration-photo-label">Selfie photo <span class="optional">Optional</span></span><button type="button" class="button" data-photo-start>Take photo</button></div><label class="photo-upload-label">Upload a photo<input type="file" name="photo" accept="image/jpeg,image/png"></label><p class="field-help">Take a selfie or upload a JPG or PNG, up to 5 MB. Stored privately after registration.</p><div class="registration-photo-selected" data-photo-selected hidden><img alt="Selected employee selfie"><span>Photo ready to save</span></div><div class="registration-camera" data-photo-camera hidden><video playsinline muted aria-label="Employee selfie camera preview"></video><img data-photo-preview alt="Captured selfie preview" hidden><div class="registration-camera-status" data-photo-status role="status"></div><div class="registration-camera-actions"><button type="button" class="button primary" data-photo-capture>Take photo</button><button type="button" class="button primary" data-photo-use hidden>Use photo</button><button type="button" class="button" data-photo-retake hidden>Retake</button><button type="button" class="button" data-photo-cancel>Cancel</button></div></div><p class="registration-photo-error" data-photo-error role="alert" hidden></p></section>`;
}

export function attachEmployeePhotoLifecycle(controller, { dialog, container, document, window, observerFactory = callback => new MutationObserver(callback) }) {
  let disposed = false;
  const close = () => {
    if (disposed) return;
    disposed = true;
    controller.dispose();
    dialog.removeEventListener("close", close);
    dialog.removeEventListener("cancel", close);
    window.removeEventListener("pagehide", close);
    window.removeEventListener("hashchange", close);
    document.removeEventListener("visibilitychange", hidden);
    document.removeEventListener("click", leaving, true);
    observer.disconnect();
  };
  const hidden = () => { if (document.hidden) controller.suspend(); };
  const leaving = event => {
    if (event.target?.closest?.("[data-close-modal], #logout, #exit-workspace") || event.target === dialog) close();
  };
  const observer = observerFactory(() => { if (!container.isConnected || !dialog.open) close(); });
  observer.observe(dialog, { childList: true, subtree: true, attributes: true, attributeFilter: ["open"] });
  dialog.addEventListener("close", close);
  dialog.addEventListener("cancel", close);
  window.addEventListener("pagehide", close);
  window.addEventListener("hashchange", close);
  document.addEventListener("visibilitychange", hidden);
  document.addEventListener("click", leaving, true);
  return close;
}

export function bindEmployeePhoto(dialog) {
  const container = dialog.querySelector(".registration-photo");
  const video = container.querySelector("video");
  const upload = container.querySelector('input[name="photo"]');
  const error = container.querySelector("[data-photo-error]");
  const selected = container.querySelector("[data-photo-selected]");
  const cameraPanel = container.querySelector("[data-photo-camera]");
  const preview = container.querySelector("[data-photo-preview]");
  const status = container.querySelector("[data-photo-status]");
  const start = container.querySelector("[data-photo-start]");
  const capture = container.querySelector("[data-photo-capture]");
  const use = container.querySelector("[data-photo-use]");
  const retake = container.querySelector("[data-photo-retake]");
  const cancel = container.querySelector("[data-photo-cancel]");
  let busy = false;
  let controller;

  function render() {
    if (!controller || controller.disposed || !container.isConnected) return;
    const editing = controller.mode !== "idle";
    const captured = controller.mode === "preview";
    cameraPanel.hidden = !editing;
    video.hidden = captured;
    preview.hidden = !captured;
    if (controller.candidateUrl) preview.src = controller.candidateUrl;
    else preview.removeAttribute("src");
    selected.hidden = !controller.photoUrl || editing;
    if (controller.photoUrl) selected.querySelector("img").src = controller.photoUrl;
    else selected.querySelector("img").removeAttribute("src");
    start.disabled = busy || editing;
    upload.disabled = busy || editing;
    capture.hidden = captured;
    capture.disabled = busy || controller.camera.state !== "active" || controller.mode === "capturing";
    use.hidden = !captured;
    retake.hidden = !captured;
    use.disabled = busy;
    retake.disabled = busy;
    cancel.disabled = busy;
    status.textContent = captured ? "Review your photo before using it." : controller.mode === "capturing" ? "Preparing photo…" : controller.camera.state === "starting" ? "Starting camera…" : "Position your face in the preview.";
  }

  function clearError() { error.hidden = true; error.textContent = ""; }
  function showError(failure) { error.textContent = photoCameraError(failure); error.hidden = false; }
  async function run(action) {
    if (busy || controller.disposed) return;
    clearError();
    try { await action(); }
    catch (failure) { if (!controller.disposed) showError(failure); }
  }

  controller = new EmployeePhotoCapture({ video, canvas: document.createElement("canvas"), mediaDevices: navigator.mediaDevices, secureContext: window.isSecureContext, onChange: render });
  const dispose = attachEmployeePhotoLifecycle(controller, { dialog, container, document, window });
  start.onclick = () => run(() => controller.start());
  capture.onclick = () => run(() => controller.capture());
  retake.onclick = () => run(() => controller.start());
  cancel.onclick = () => run(() => controller.cancel());
  use.onclick = () => run(() => { if (controller.usePhoto()) upload.value = ""; });
  upload.onchange = () => run(() => {
    const file = upload.files?.[0];
    if (!file) return;
    try { controller.select(file); }
    catch (failure) { upload.value = ""; throw failure; }
  });
  render();
  return {
    get photo() {
      if (controller.mode !== "idle") throw new Error("Choose Use photo or Cancel before registering the employee.");
      return controller.photo;
    },
    setBusy(value) {
      busy = value;
      if (busy) controller.suspend();
      render();
    },
    dispose,
  };
}
