import { api } from "./api.js";
import { escape, timestamp } from "./core.js";
import { badge, closeModal, empty, errorMessage, field, formAction, heading, modal, notify, table } from "./ui.js";

const filters = [["all", "All"], ["active", "Active"], ["flagged", "Flagged"], ["ended", "Ended"]];
const endReasons = { SIGNED_OUT: "Signed out", REPLACED: "New sign-in on the phone", ENDED_BY_ADMIN: "Ended by admin" };

export function mapLink(latitude, longitude) {
  const lat = Number(latitude).toFixed(6);
  const lon = Number(longitude).toFixed(6);
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=18/${lat}/${lon}`;
}

export function accuracyLabel(meters) {
  const value = meters === null || meters === undefined ? Number.NaN : Number(meters);
  return Number.isFinite(value) ?`±${Math.round(value)} m` : "—";
}

export function flagLabel(shift) {
  if (shift.flagged === null || shift.flagged === undefined) return badge("Canteen not set");
  const distance = Number(shift.distance_m);
  const text = Number.isFinite(distance) ? `${Math.round(distance)} m away` : "";
  return shift.flagged ? badge(`Flagged · ${text}`, "danger") : badge(`At canteen · ${text}`, "positive");
}

export function statusLabel(shift) {
  if (shift.status === "ACTIVE") return badge("Active", "positive");
  if (shift.status === "EXPIRED") return badge("Ended · shift time over");
  return badge(`Ended · ${endReasons[shift.end_reason] ?? "ended"}`);
}

export function canteenPayload(values) {
  const fields = ["latitude", "longitude", "radius_m"].map(key => String(values[key] ?? "").trim());
  if (fields.every(value => value === "")) return { latitude: null, longitude: null, radius_m: null };
  if (fields.some(value => value === "")) throw new Error("Enter latitude, longitude and radius together, or clear all three.");
  const [latitude, longitude, radius] = fields.map(Number);
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) throw new Error("Latitude must be between -90 and 90.");
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) throw new Error("Longitude must be between -180 and 180.");
  if (!Number.isInteger(radius) || radius < 10 || radius > 50000) throw new Error("Radius must be a whole number of metres from 10 to 50000.");
  return { latitude, longitude, radius_m: radius };
}

function shiftRow(shift) {
  const selfie = `<img class="shift-selfie" src="/api/waiter-shifts/${encodeURIComponent(shift.id)}/selfie" alt="Selfie of ${escape(shift.waiter_name)}" loading="lazy" data-selfie>`;
  const location = `<a href="${escape(mapLink(shift.latitude, shift.longitude))}" target="_blank" rel="noopener noreferrer">${escape(Number(shift.latitude).toFixed(5))}, ${escape(Number(shift.longitude).toFixed(5))}</a>`;
  const end = shift.status === "ACTIVE" ? `<button type="button" class="small-button" data-end-shift="${escape(shift.id)}" data-name="${escape(shift.waiter_name)}">End shift now</button>` : "";
  return `<tr><td>${selfie}</td><td><strong>${escape(shift.waiter_name)}</strong></td><td class="nowrap">${escape(timestamp(shift.started_at))}<span class="cell-subtitle">until ${escape(timestamp(shift.ended_at ?? shift.expires_at))}</span></td><td>${escape(shift.phone_label ?? shift.phone_id)}</td><td>${location}</td><td>${escape(accuracyLabel(shift.accuracy_m))}</td><td>${flagLabel(shift)}</td><td>${statusLabel(shift)}</td><td>${end}</td></tr>`;
}

export async function waiterShifts(target, context) {
  let view = "all";
  let rows = [];
  let cursor = null;
  target.innerHTML = `${heading("SCANNER STAFF", "Waiter shifts", "Who signed in on each scanner phone, with their selfie, location and accuracy. Sign-ins outside the canteen radius are flagged for review.")}<section class="panel"><div class="panel-heading"><div><h2>Canteen location</h2><p class="muted small" id="canteen-summary"></p></div></div><form id="canteen-form" class="form-grid">${field("Latitude", "latitude", "", "text", 'inputmode="decimal"')}${field("Longitude", "longitude", "", "text", 'inputmode="decimal"')}${field("Radius (metres)", "radius_m", "", "number", 'min="10" max="50000" step="1"')}<div data-form-error></div><div class="form-actions"><button type="button" class="button" id="canteen-here">Use this computer's location</button><button type="submit" class="button primary">Save canteen location</button></div></form></section><section class="panel"><div class="status-filter" role="group" aria-label="Shift filter">${filters.map(([key, label]) => `<button type="button" data-shift-view="${key}" class="${key === view ? "active" : ""}" aria-pressed="${key === view}">${label}</button>`).join("")}</div><div id="shift-list"></div><div class="pagination" id="shift-pagination"></div></section>`;
  const form = target.querySelector("#canteen-form");
  async function loadSettings() {
    const settings = await api("/waiter-shift-settings");
    if (!context.isCurrent()) return;
    form.elements.latitude.value = settings.latitude ?? "";
    form.elements.longitude.value = settings.longitude ?? "";
    form.elements.radius_m.value = settings.radius_m ?? "";
    target.querySelector("#canteen-summary").textContent = settings.configured ? `Sign-ins more than ${settings.radius_m} m from this point are flagged.` : "Not set yet: sign-ins are recorded but not flagged.";
  }
  formAction(form, async data => {
    await api("/waiter-shift-settings", { method: "PUT", body: canteenPayload(Object.fromEntries(data)) });
    notify("Canteen location saved.");
    await loadSettings();
  });
  target.querySelector("#canteen-here").onclick = () => {
    if (!navigator.geolocation) { notify("Location is unavailable in this browser.", "error"); return; }
    navigator.geolocation.getCurrentPosition(position => {
      form.elements.latitude.value = position.coords.latitude.toFixed(6);
      form.elements.longitude.value = position.coords.longitude.toFixed(6);
      if (!form.elements.radius_m.value) form.elements.radius_m.value = "100";
    }, () => notify("Location permission was refused or unavailable.", "error"), { enableHighAccuracy: true, timeout: 20000 });
  };
  async function load(append = false) {
    const data = await api(`/waiter-shifts?status=${view}&limit=50${append && cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`);
    if (!context.isCurrent()) return;
    rows = append ? rows.concat(data.items ?? []) : (data.items ?? []);
    cursor = data.next_cursor;
    target.querySelector("#shift-list").innerHTML = rows.length ? table(["Selfie", "Name", "Signed in", "Phone", "Location", "Accuracy", "Canteen check", "Status", ""], rows.map(shiftRow), "Waiter shifts") : empty("No shifts", "Shifts appear here when waiters sign in on scanner phones.", "people");
    target.querySelector("#shift-pagination").innerHTML = `<span>${rows.length} shifts shown</span>${cursor ? '<button class="button" id="more-shifts">Load more</button>' : ""}`;
    target.querySelector("#more-shifts")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
    target.querySelectorAll("[data-selfie]").forEach(image => image.addEventListener("error", () => {
      image.replaceWith(Object.assign(document.createElement("span"), { className: "muted small", textContent: "Selfie expired" }));
    }, { once: true }));
    target.querySelectorAll("[data-end-shift]").forEach(button => button.onclick = () => {
      const dialog = modal("End shift now", `<p>End ${escape(button.dataset.name)}'s shift now? The scanner phone will ask them to sign in again.</p><form><div data-form-error></div><div class="form-actions"><button type="submit" class="button danger-outline">End shift now</button></div></form>`);
      formAction(dialog.querySelector("form"), async () => {
        await api(`/waiter-shifts/${encodeURIComponent(button.dataset.endShift)}/end`, { method: "POST" });
        closeModal();
        notify("Shift ended.");
        await load();
      });
    });
  }
  target.querySelectorAll("[data-shift-view]").forEach(button => button.onclick = () => {
    view = button.dataset.shiftView;
    target.querySelectorAll("[data-shift-view]").forEach(item => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); });
    load().catch(error => notify(error.message, "error"));
  });
  try {
    await Promise.all([loadSettings(), load()]);
  } catch (error) {
    if (context.isCurrent()) target.querySelector("#shift-list").innerHTML = errorMessage(error);
  }
}
