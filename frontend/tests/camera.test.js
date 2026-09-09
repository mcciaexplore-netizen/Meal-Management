import assert from "node:assert/strict";
import test from "node:test";
import { CameraSession, cameraError } from "../src/camera.js";

function stream(deviceId = "rear", facingMode = "environment") {
  const track = { stopped: 0, stop() { this.stopped += 1; }, getSettings() { return { deviceId, facingMode }; } };
  return { track, getTracks: () => [track], getVideoTracks: () => [track] };
}

function fixture(overrides = {}) {
  const calls = [];
  const media = stream();
  const video = { srcObject: null, play: async () => {} };
  const devices = {
    getUserMedia: async constraints => { calls.push(constraints); return media; },
    enumerateDevices: async () => [{ kind: "videoinput", deviceId: "rear" }, { kind: "videoinput", deviceId: "front" }],
    ...overrides
  };
  return { media, video, calls, camera: new CameraSession({ video, mediaDevices: devices, secureContext: true }) };
}

test("camera startup prefers rear video and never requests audio", async () => {
  const { camera, calls, media, video } = fixture();
  assert.equal(await camera.start(), true);
  assert.deepEqual(calls[0].video.facingMode, { ideal: "environment" });
  assert.equal(calls[0].audio, false);
  assert.equal(camera.state, "active");
  assert.equal(video.srcObject, media);
  camera.stop();
  assert.equal(media.track.stopped, 1);
  assert.equal(video.srcObject, null);
});

test("selfie callers can prefer the front camera without changing the scanner default", async () => {
  let constraints;
  const media = stream("front", "user");
  const camera = new CameraSession({
    video: { play: async () => {} }, secureContext: true, facingMode: "user",
    mediaDevices: { getUserMedia: async values => { constraints = values; return media; } },
  });
  await camera.start();
  assert.deepEqual(constraints.video.facingMode, { ideal: "user" });
  assert.equal(constraints.audio, false);
  camera.dispose();
  assert.equal(media.track.stopped, 1);
});

test("switching releases the old camera before requesting the next device", async () => {
  const first = stream("rear");
  const second = stream("front", "user");
  let calls = 0;
  const { camera } = fixture({ getUserMedia: async constraints => {
    calls += 1;
    if (calls === 1) return first;
    assert.equal(first.track.stopped, 1);
    assert.deepEqual(constraints.video.deviceId, { exact: "front" });
    return second;
  } });
  await camera.start();
  assert.equal(await camera.switch(), true);
  assert.equal(camera.deviceId, "front");
  camera.dispose();
  assert.equal(second.track.stopped, 1);
});

test("stopping while permission is pending releases late-arriving camera tracks", async () => {
  let deliver;
  const media = stream();
  const { camera, video } = fixture({ getUserMedia: () => new Promise(resolve => { deliver = resolve; }) });
  const starting = camera.start();
  camera.stop();
  deliver(media);
  assert.equal(await starting, false);
  assert.equal(media.track.stopped, 1);
  assert.equal(video.srcObject, null);
  assert.equal(camera.state, "stopped");
});

test("disposing during video playback startup cannot revive the camera", async () => {
  let play;
  const { camera, video, media } = fixture();
  video.play = () => new Promise(resolve => { play = resolve; });
  const starting = camera.start();
  await Promise.resolve();
  camera.dispose();
  play();
  assert.equal(await starting, false);
  assert.ok(media.track.stopped >= 1);
  assert.equal(video.srcObject, null);
  assert.equal(await camera.start(), false);
});

test("stale startup responses cannot replace a newer camera", async () => {
  const pending = [];
  const { camera, video } = fixture({ getUserMedia: () => new Promise(resolve => pending.push(resolve)) });
  const older = camera.start();
  const newer = camera.start({ deviceId: "front" });
  const oldMedia = stream("rear");
  const newMedia = stream("front");
  pending[1](newMedia);
  await newer;
  pending[0](oldMedia);
  assert.equal(await older, false);
  assert.equal(oldMedia.track.stopped, 1);
  assert.equal(video.srcObject, newMedia);
  camera.dispose();
});

test("playback failure releases acquired camera tracks", async () => {
  const { camera, video, media } = fixture();
  video.play = async () => { throw new Error("Playback blocked"); };
  await assert.rejects(camera.start(), /Playback blocked/);
  assert.equal(media.track.stopped, 1);
  assert.equal(video.srcObject, null);
});

test("insecure origins and unsupported browsers do not request camera access", async () => {
  const { camera, calls, video } = fixture();
  camera.secureContext = false;
  await assert.rejects(camera.start(), /HTTPS or localhost/);
  assert.equal(calls.length, 0);
  const unsupported = new CameraSession({ video, mediaDevices: {}, secureContext: true });
  await assert.rejects(unsupported.start(), /does not support/);
});

test("camera failure messages distinguish denial, missing devices, and unavailable devices", () => {
  assert.match(cameraError({ name: "NotAllowedError" }), /permission was denied/);
  assert.match(cameraError({ name: "NotFoundError" }), /No camera/);
  assert.match(cameraError({ name: "NotReadableError" }), /another app/);
});

test("switching falls back to front facing mode when device enumeration is unavailable", async () => {
  const { camera, calls } = fixture({ enumerateDevices: async () => { throw new Error("Unsupported"); } });
  await camera.start();
  await camera.switch();
  assert.deepEqual(calls[1].video.facingMode, { ideal: "user" });
  camera.dispose();
});
