import { escape } from "./core.js";

export const NOTICE_VERSION = "2026-10-26";
export const SHIFT_NOTICE = "To confirm who served meals and where, this scanner records your name, a selfie and this phone's location when you sign in for a shift. Selfies are stored privately and deleted after 90 days; the sign-in record is kept. Location is used only to check you signed in at the canteen.";
export const WARNING_MINUTES = 10;

export function shiftState(shift, now = new Date()) {
  if (!shift?.expires_at) return { active: false, warning: false, minutesLeft: 0 };
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(shift.expires_at) ? shift.expires_at : `${shift.expires_at}Z`;
  const remaining = new Date(normalized).getTime() - now.getTime();
  if (!Number.isFinite(remaining) || remaining <= 0) return { active: false, warning: false, minutesLeft: 0 };
  const minutesLeft = Math.ceil(remaining / 60000);
  return { active: true, warning: remaining <= WARNING_MINUTES * 60000, minutesLeft, remainingMs: remaining };
}

export function shiftEndTime(shift) {
  if (!shift?.expires_at) return "";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(shift.expires_at) ? shift.expires_at : `${shift.expires_at}Z`;
  const parsed = new Date(normalized);
  if (!Number.isFinite(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }).format(parsed);
}

export function signInPayload({ name, consent, selfie, position }) {
  const waiterName = String(name ?? "").trim();
  if (!waiterName || waiterName.length > 150) throw new Error("Enter your name.");
  if (consent !== true) throw new Error("Tick “I agree” to sign in.");
  if (typeof selfie !== "string" || !selfie || !/^[A-Za-z0-9+/]+=*$/.test(selfie)) throw new Error("Take a selfie to sign in.");
  const coords = position?.coords;
  const numbers = [coords?.latitude, coords?.longitude, coords?.accuracy];
  if (!numbers.every(value => typeof value === "number" && Number.isFinite(value))) throw new Error("Share this phone's location to sign in.");
  return {
    waiter_name: waiterName,
    latitude: coords.latitude,
    longitude: coords.longitude,
    accuracy_m: Math.max(0, coords.accuracy),
    consent: true,
    notice_version: NOTICE_VERSION,
    selfie
  };
}

export function locationError(error) {
  if (error?.code === 1) return "Location permission is needed to sign in. Allow location for this site in the browser settings, then try again.";
  if (error?.code === 3) return "Finding this phone's location took too long. Move near a window and try again.";
  return "This phone's location is unavailable. Turn on location services and try again.";
}

export function requestPosition(geolocation = globalThis.navigator?.geolocation) {
  return new Promise((resolve, reject) => {
    if (!geolocation?.getCurrentPosition) { reject({ code: 2 }); return; }
    geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
  });
}

export async function shrinkSelfie(file, { maxSide = 640, quality = 0.8, documentRef = globalThis.document } = {}) {
  if (!file || !/^image\//.test(file.type ?? "")) throw new Error("Take a selfie with the phone camera.");
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = documentRef.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const dataUrl = canvas.toDataURL("image/jpeg", quality);
  return { preview: dataUrl, base64: dataUrl.slice(dataUrl.indexOf(",") + 1) };
}

export function signInForm(previousName = "") {
  return `<main id="main" class="startup shift-signin"><h1>Sign in for your shift</h1><p class="shift-notice" id="shift-notice">${escape(SHIFT_NOTICE)}</p><form id="shift-signin-form" novalidate><label class="consent"><input type="checkbox" name="consent" required aria-describedby="shift-notice"> I agree</label><label>Your name<input name="waiter_name" maxlength="150" autocomplete="name" required value="${escape(previousName)}"></label><div class="selfie-field"><label class="button" for="shift-selfie">Take selfie</label><input id="shift-selfie" type="file" accept="image/*" capture="user" hidden><img id="selfie-preview" alt="Your selfie" hidden></div><p class="field-help">Location is requested when you sign in. Signing in needs location permission.</p><div data-form-error></div><button class="button primary full" type="submit">Sign in</button></form></main>`;
}
