import assert from "node:assert/strict";
import test from "node:test";
import { attachEmployeePhotoLifecycle, captureEmployeePhoto, EmployeePhotoCapture, employeePhotoField, photoCameraError, validateEmployeePhoto } from "../src/employee_photo.js";

const jpeg = () => new Blob([new Uint8Array([255, 216, 255, 217])], { type: "image/jpeg" });
const png = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });

function fixture(options = {}) {
  const tracks = [];
  const calls = [];
  const revoked = [];
  const created = [];
  const video = { srcObject: null, readyState: 2, videoWidth: 1920, videoHeight: 1080, play: async () => {} };
  const canvas = { getContext: () => ({ drawImage: (...values) => calls.push(values) }), toBlob: (callback, type, quality) => { calls.push({ type, quality }); callback(jpeg()); } };
  const mediaDevices = { getUserMedia: async constraints => {
    calls.push(constraints);
    const track = { stopped: 0, stop() { this.stopped += 1; }, getSettings: () => ({ facingMode: "user" }) };
    tracks.push(track);
    return { getTracks: () => [track], getVideoTracks: () => [track] };
  } };
  const urls = { createObjectURL: photo => { created.push(photo); return `blob:photo-${created.length}`; }, revokeObjectURL: url => revoked.push(url) };
  const controller = new EmployeePhotoCapture({ video, canvas, mediaDevices, secureContext: true, urls, ...options });
  return { controller, video, canvas, tracks, calls, revoked, created };
}

test("registration photo controls retain upload and require explicit capture review", () => {
  const html = employeePhotoField();
  for (const label of ["Take photo", "Use photo", "Retake", "Cancel", "Upload a photo"]) assert.ok(html.includes(label));
  assert.match(html, /accept="image\/jpeg,image\/png"/);
  assert.match(html, /playsinline muted/);
  assert.doesNotMatch(html, /type="submit"|src="https?:/);
});

test("employee camera starts with front preference and capture produces a bounded JPEG", async () => {
  const { controller, canvas, calls, tracks } = fixture();
  await controller.start();
  assert.deepEqual(calls[0].video.facingMode, { ideal: "user" });
  assert.equal(calls[0].audio, false);
  assert.equal(await controller.capture(), true);
  assert.equal(canvas.width, 1280);
  assert.equal(canvas.height, 720);
  assert.equal(controller.candidate.type, "image/jpeg");
  assert.equal(controller.photo, null);
  assert.equal(controller.mode, "preview");
  assert.equal(tracks[0].stopped, 1);
  assert.deepEqual(calls.at(-1), { type: "image/jpeg", quality: 0.9 });
  controller.dispose();
});

test("use photo transfers the preview blob and releases an earlier selected photo", async () => {
  const { controller, revoked, created } = fixture();
  controller.select(png());
  await controller.start();
  await controller.capture();
  const captured = controller.candidate;
  assert.equal(controller.usePhoto(), true);
  assert.equal(controller.photo, captured);
  assert.equal(controller.photoUrl, "blob:photo-2");
  assert.equal(controller.candidate, null);
  assert.equal(controller.mode, "idle");
  assert.deepEqual(revoked, ["blob:photo-1"]);
  assert.equal(created.length, 2);
  controller.dispose();
  assert.deepEqual(revoked, ["blob:photo-1", "blob:photo-2"]);
});

test("cancel preserves the previous upload while discarding a captured candidate", async () => {
  const { controller, revoked, tracks } = fixture();
  const original = png();
  controller.select(original);
  await controller.start();
  await controller.capture();
  controller.cancel();
  assert.equal(controller.photo, original);
  assert.equal(controller.photoUrl, "blob:photo-1");
  assert.equal(controller.candidate, null);
  assert.deepEqual(revoked, ["blob:photo-2"]);
  assert.equal(tracks[0].stopped, 1);
  controller.dispose();
});

test("retake releases only the temporary preview and obtains a new front camera stream", async () => {
  const { controller, revoked, tracks } = fixture();
  const original = png();
  controller.select(original);
  await controller.start();
  await controller.capture();
  await controller.start();
  assert.equal(tracks.length, 2);
  assert.deepEqual(revoked, ["blob:photo-2"]);
  assert.equal(controller.photo, original);
  controller.cancel();
  assert.equal(tracks[1].stopped, 1);
  controller.dispose();
});

test("closing while camera permission is pending releases the late camera without a preview", async () => {
  let deliver;
  const track = { stopped: 0, stop() { this.stopped += 1; } };
  const { controller, video, created } = fixture({ mediaDevices: { getUserMedia: () => new Promise(resolve => { deliver = resolve; }) } });
  const starting = controller.start();
  controller.dispose();
  deliver({ getTracks: () => [track] });
  assert.equal(await starting, false);
  assert.equal(track.stopped, 1);
  assert.equal(video.srcObject, null);
  assert.equal(created.length, 0);
});

test("closing during JPEG encoding cannot create a late preview URL", async () => {
  const { controller, canvas, tracks, created } = fixture();
  let complete;
  canvas.toBlob = callback => { complete = callback; };
  await controller.start();
  const capturing = controller.capture();
  controller.dispose();
  complete(jpeg());
  assert.equal(await capturing, false);
  assert.equal(created.length, 0);
  assert.equal(tracks[0].stopped, 1);
});

test("empty or unavailable camera frames cannot become selected photos", async () => {
  const { video, canvas } = fixture();
  await assert.rejects(captureEmployeePhoto({ ...video, readyState: 1 }, canvas), /still starting/);
  await assert.rejects(captureEmployeePhoto({ ...video, videoWidth: 0 }, canvas), /still starting/);
  await assert.rejects(captureEmployeePhoto(video, { getContext: () => null }), /cannot capture/);
  canvas.toBlob = callback => callback(null);
  await assert.rejects(captureEmployeePhoto(video, canvas), /photo was empty/);
  canvas.toBlob = callback => callback(new Blob([], { type: "image/jpeg" }));
  await assert.rejects(captureEmployeePhoto(video, canvas), /photo was empty/);
  canvas.toBlob = callback => callback(png());
  await assert.rejects(captureEmployeePhoto(video, canvas), /could not create a JPEG/);
});

test("capture errors release camera tracks and retain the earlier selected upload", async () => {
  const { controller, canvas, tracks } = fixture();
  const original = png();
  controller.select(original);
  canvas.toBlob = callback => callback(null);
  await controller.start();
  await assert.rejects(controller.capture(), /photo was empty/);
  assert.equal(tracks[0].stopped, 1);
  assert.equal(controller.photo, original);
  assert.equal(controller.mode, "idle");
  controller.dispose();
});

test("photo validation rejects empty unsupported and oversized files without replacing the selection", () => {
  const { controller, created } = fixture();
  const original = jpeg();
  controller.select(original);
  for (const photo of [new Blob([], { type: "image/jpeg" }), new Blob([1], { type: "image/svg+xml" }), new Blob([new Uint8Array(5 * 1024 * 1024 + 1)], { type: "image/jpeg" })]) assert.throws(() => controller.select(photo));
  assert.equal(controller.photo, original);
  assert.equal(created.length, 1);
  assert.doesNotThrow(() => validateEmployeePhoto(null));
  controller.dispose();
});

test("camera denial and insecure connection use photo-specific fallback messages", async () => {
  assert.match(photoCameraError({ name: "NotAllowedError" }), /permission was denied/);
  assert.doesNotMatch(photoCameraError({ name: "NotAllowedError" }), /QR/);
  const { controller } = fixture({ secureContext: false });
  await assert.rejects(controller.start(), error => /HTTPS or localhost/.test(photoCameraError(error)) && !/QR/.test(photoCameraError(error)));
  assert.equal(controller.mode, "idle");
  controller.dispose();
});

function lifecycleFixture(controller) {
  const dialog = new EventTarget();
  const document = new EventTarget();
  const window = new EventTarget();
  const container = { isConnected: true };
  dialog.open = true;
  let observe;
  let observedOptions;
  let disconnected = false;
  const close = attachEmployeePhotoLifecycle(controller, {
    dialog, document, window, container,
    observerFactory: callback => { observe = callback; return { observe(target, options) { observedOptions = options; }, disconnect() { disconnected = true; } }; },
  });
  return { dialog, document, window, container, close, observe: () => observe(), nestedRemoval: () => { container.isConnected = false; if (observedOptions.subtree) observe(); }, disconnected: () => disconnected };
}

test("hiding the page stops capture while preserving the selected photo", async () => {
  const { controller, tracks } = fixture();
  const original = jpeg();
  controller.select(original);
  const lifecycle = lifecycleFixture(controller);
  await controller.start();
  lifecycle.document.hidden = true;
  lifecycle.document.dispatchEvent(new Event("visibilitychange"));
  assert.equal(tracks[0].stopped, 1);
  assert.equal(controller.photo, original);
  assert.equal(controller.mode, "idle");
  lifecycle.close();
});

test("modal close navigation page departure and content replacement dispose all photo URLs", async () => {
  for (const leave of [value => value.dialog.dispatchEvent(new Event("close")), value => value.dialog.dispatchEvent(new Event("cancel")), value => value.window.dispatchEvent(new Event("hashchange")), value => value.window.dispatchEvent(new Event("pagehide")), value => { value.container.isConnected = false; value.observe(); }]) {
    const { controller, tracks, revoked } = fixture();
    controller.select(jpeg());
    const lifecycle = lifecycleFixture(controller);
    await controller.start();
    leave(lifecycle);
    assert.equal(controller.disposed, true);
    assert.equal(tracks[0].stopped, 1);
    assert.deepEqual(revoked, ["blob:photo-1"]);
    assert.equal(lifecycle.disconnected(), true);
  }
});

test("logout click disposes photo capture before a network logout could run", async () => {
  const { controller, tracks } = fixture();
  const lifecycle = lifecycleFixture(controller);
  await controller.start();
  const event = new Event("click");
  Object.defineProperty(event, "target", { value: { closest: selector => selector.includes("#logout") } });
  lifecycle.document.dispatchEvent(event);
  assert.equal(controller.disposed, true);
  assert.equal(tracks[0].stopped, 1);
});

test("nested modal content replacement releases the camera while the dialog remains open", async () => {
  const { controller, tracks, revoked } = fixture();
  controller.select(jpeg());
  const lifecycle = lifecycleFixture(controller);
  await controller.start();
  lifecycle.nestedRemoval();
  assert.equal(lifecycle.dialog.open, true);
  assert.equal(controller.disposed, true);
  assert.equal(tracks[0].stopped, 1);
  assert.deepEqual(revoked, ["blob:photo-1"]);
});
