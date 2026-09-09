export function cameraError(error) {
  const messages = {
    NotAllowedError: "Camera permission was denied. Allow camera access in your browser, or upload a QR image.",
    SecurityError: "This browser blocked camera access. Use a secure connection or upload a QR image.",
    NotFoundError: "No camera was found on this device. Upload a QR image instead.",
    DevicesNotFoundError: "No camera was found on this device. Upload a QR image instead.",
    NotReadableError: "The camera is unavailable or being used by another app. Close the other app, or upload a QR image.",
    TrackStartError: "The camera could not start. Close other camera apps, or upload a QR image.",
    OverconstrainedError: "The selected camera is unavailable. Try switching cameras, or upload a QR image."
  };
  return messages[error?.name] ?? error?.message ?? "The camera could not start. Upload a QR image instead.";
}

export class CameraSession {
  constructor({ video, mediaDevices, secureContext, onState = () => {}, facingMode = "environment" }) {
    this.video = video;
    this.mediaDevices = mediaDevices;
    this.secureContext = secureContext;
    this.onState = onState;
    this.stream = null;
    this.devices = [];
    this.deviceId = null;
    this.facingMode = facingMode;
    this.epoch = 0;
    this.disposed = false;
    this.state = "stopped";
  }

  changeState(state) {
    this.state = state;
    this.onState(state);
  }

  stop() {
    this.epoch += 1;
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
    this.video.srcObject = null;
    this.changeState("stopped");
  }

  dispose() {
    this.disposed = true;
    this.stop();
  }

  async start({ deviceId = this.deviceId, facingMode = this.facingMode } = {}) {
    if (this.disposed) return false;
    this.stop();
    if (!this.secureContext) throw new Error("Live camera scanning requires HTTPS or localhost. You can upload a QR image on this connection.");
    if (!this.mediaDevices?.getUserMedia) throw new Error("This browser does not support live camera access. Upload a QR image instead.");
    const epoch = this.epoch;
    this.changeState("starting");
    let media;
    try {
      media = await this.mediaDevices.getUserMedia({
        video: {
          ...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: { ideal: facingMode } }),
          width: { ideal: 1280 },
          height: { ideal: 720 }
        },
        audio: false
      });
      if (this.disposed || epoch !== this.epoch) {
        media.getTracks().forEach(track => track.stop());
        return false;
      }
      this.stream = media;
      this.video.srcObject = media;
      await this.video.play();
      if (this.disposed || epoch !== this.epoch) {
        media.getTracks().forEach(track => track.stop());
        return false;
      }
      const settings = media.getVideoTracks?.()[0]?.getSettings?.() ?? {};
      this.deviceId = settings.deviceId ?? deviceId;
      this.facingMode = settings.facingMode ?? facingMode;
      try {
        this.devices = (await this.mediaDevices.enumerateDevices?.() ?? []).filter(device => device.kind === "videoinput");
      } catch {
        this.devices = [];
      }
      if (this.disposed || epoch !== this.epoch) return false;
      this.changeState("active");
      return true;
    } catch (error) {
      if (this.disposed || epoch !== this.epoch) return false;
      this.stop();
      throw error;
    }
  }

  async switch() {
    if (this.disposed) return false;
    const index = this.devices.findIndex(device => device.deviceId === this.deviceId);
    const next = this.devices.length > 1 ? this.devices[(index + 1) % this.devices.length].deviceId : null;
    return this.start({ deviceId: next, facingMode: this.facingMode === "environment" ? "user" : "environment" });
  }
}
