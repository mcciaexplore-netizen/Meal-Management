import jsQR from "jsqr";
import { qrImageFile } from "./scan_helpers.js";

export function decodePixels(pixels) {
  return jsQR(pixels.data, pixels.width, pixels.height, { inversionAttempts: "attemptBoth" })?.data ?? null;
}

export async function nativeQrDetector(Detector = globalThis.BarcodeDetector) {
  if (!Detector) return null;
  try {
    const supported = await Detector.getSupportedFormats();
    return supported.includes("qr_code") ? new Detector({ formats: ["qr_code"] }) : null;
  } catch {
    return null;
  }
}

export async function decodeSource(source, canvas, detector = null, maximum = 1600) {
  if (detector) {
    try {
      const values = await detector.detect(source);
      if (values[0]?.rawValue) return values[0].rawValue;
    } catch {}
  }
  const width = source.videoWidth || source.naturalWidth || source.width;
  const height = source.videoHeight || source.naturalHeight || source.height;
  if (!width || !height) return null;
  const factor = Math.min(1, maximum / Math.max(width, height));
  canvas.width = Math.max(1, Math.round(width * factor));
  canvas.height = Math.max(1, Math.round(height * factor));
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("QR image processing is unavailable in this browser.");
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return decodePixels(context.getImageData(0, 0, canvas.width, canvas.height));
}

export async function decodeImage(file, canvas, detector = null) {
  qrImageFile(file);
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("This image could not be opened. Choose a valid QR image."));
      image.src = url;
    });
    const token = await decodeSource(image, canvas, detector, 2000);
    if (!token) throw new Error("No readable QR was found. Choose a clear, uncropped image of a single QR.");
    return token;
  } finally {
    URL.revokeObjectURL(url);
  }
}
