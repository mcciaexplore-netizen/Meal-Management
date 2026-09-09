import { escape, timestamp } from "./core.js";

export const icons = {
  grid: '<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>',
  people: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
  qr: '<path d="M3 3h6v6H3zM15 3h6v6h-6zM3 15h6v6H3zM15 15h2v2h-2zM21 15v6h-6M12 3v4m0 5h9M3 12h5m4 3v6"/>',
  scan: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 12h18"/>',
  chart: '<path d="M3 3v18h18M7 16v-4m5 4V7m5 9V4"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
  settings: '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  logout: '<path d="M9 21H3V3h6m7 4 5 5-5 5M8 12h13"/>',
  plate: '<circle cx="12" cy="12" r="6"/><path d="M2 3v7m3-7v7m-3-3h3M3.5 10v11M21 3v18m0-18c-4 2-4 9 0 9"/>'
};

export function icon(name, className = "") {
  return `<svg class="icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] ?? icons.grid}</svg>`;
}

export function notify(message, type = "success") {
  const holder = document.getElementById("notifications");
  const item = document.createElement("div");
  item.className = `notification ${type}`;
  item.textContent = message;
  holder.append(item);
  setTimeout(() => item.remove(), 6000);
}

export function errorMessage(error) {
  return `<div class="alert error" role="alert">${escape(error.message ?? "Something went wrong. Please try again.")}</div>`;
}

export function heading(eyebrow, title, description, action = "") {
  return `<header class="page-heading"><div><p class="eyebrow">${escape(eyebrow)}</p><h1>${escape(title)}</h1><p class="muted">${escape(description)}</p></div>${action}</header>`;
}

export function empty(title, text, name = "grid") {
  return `<div class="empty">${icon(name)}<h3>${escape(title)}</h3><p>${escape(text)}</p></div>`;
}

export function loading() {
  return '<div class="loading" role="status"><span class="spinner"></span> Loading…</div>';
}

export function options(rows, selected, label = "name", value = "id") {
  return rows.map(row => `<option value="${escape(row[value] ?? row.id)}" ${String(row[value] ?? row.id) === String(selected) ? "selected" : ""}>${escape(row[label] ?? row.display_name ?? row.full_name ?? row.code)}</option>`).join("");
}

export function selectField(label, name, rows, selected = "", config = {}) {
  return `<label>${escape(label)}<select name="${escape(name)}" ${config.required === false ? "" : "required"}><option value="">${escape(config.placeholder ?? `Select ${label.toLowerCase()}`)}</option>${options(rows, selected, config.label, config.value)}</select></label>`;
}

export function field(label, name, value = "", type = "text", attrs = "") {
  return `<label>${escape(label)}<input name="${escape(name)}" type="${escape(type)}" value="${escape(value)}" ${attrs}></label>`;
}

export function badge(text, tone = "neutral") {
  return `<span class="badge ${tone}">${escape(text)}</span>`;
}

export function qrStatus(qr) {
  if (qr.credential_status === "EMPLOYEE_INACTIVE") return badge("Employee inactive", "warning");
  if (qr.revoked_at) return badge("Revoked", "danger");
  if (qr.expires_at && new Date(qr.expires_at) <= new Date()) return badge("Expired", "warning");
  return badge("Active", "positive");
}

export function qrCard(qr) {
  if (!qr?.svg) return `<div class="qr-card">${qr ? qrStatus(qr) : ""}${empty("QR display unavailable", qr?.revoked_at ? "This credential has been revoked. Issue a new QR to restore access." : qr?.expires_at && new Date(qr.expires_at) <= new Date() ? "This credential has expired. Replace it to restore access." : "An active employee and valid credential are required to display a QR.", "qr")}</div>`;
  const data = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(qr.svg)}`;
  return `<div class="qr-card"><img class="qr-image" src="${escape(data)}" alt="Private meal credential QR code"><div>${qrStatus(qr)}<p class="muted small">${qr.expires_at ? `Expires ${escape(timestamp(qr.expires_at))}` : "No scheduled expiry"}</p></div></div>`;
}

export function stats(totals) {
  const cards = [["Meals recorded", totals?.meal_count], ["Employee meals", totals?.employee_meals], ["Visitor meals", totals?.visitor_meals], ["Serving actions", totals?.serving_count]];
  return `<div class="stats">${cards.map(([name, count], index) => `<article class="stat ${index === 0 ? "featured" : ""}"><p>${name}</p><strong>${count === undefined ? "—" : Number(count).toLocaleString()}</strong><span>${index === 0 ? "Each meal, accounted for" : "Within selected dates"}</span></article>`).join("")}</div>`;
}

export function table(headers, rows, label = "Records") {
  return `<div class="table-scroll" tabindex="0" role="region" aria-label="${escape(label)}"><table><thead><tr>${headers.map(header => `<th scope="col">${escape(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

export function modal(title, body, wide = false) {
  const dialog = document.getElementById("modal");
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = `<div class="modal-header"><h2 id="modal-title">${escape(title)}</h2><button type="button" class="icon-button" data-close-modal aria-label="Close dialog">${icon("close")}</button></div><div class="modal-content">${body}</div>`;
  dialog.setAttribute("aria-labelledby", "modal-title");
  if (!dialog.open) dialog.showModal();
  dialog.querySelector("[data-close-modal]").onclick = () => closeModal();
  dialog.onclick = event => { if (event.target === dialog) closeModal(); };
  return dialog;
}

export function closeModal() {
  const dialog = document.getElementById("modal");
  dialog.close();
  dialog.innerHTML = "";
}

export function formAction(form, callback) {
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    const target = form.querySelector("[data-form-error]");
    if (target) target.innerHTML = "";
    if (button?.disabled) return;
    const label = button?.innerHTML;
    if (button) { button.disabled = true; button.textContent = "Please wait…"; }
    try {
      await callback(new FormData(form));
    } catch (error) {
      if (target) target.innerHTML = errorMessage(error);
      else notify(error.message, "error");
    } finally {
      if (button) { button.disabled = false; button.innerHTML = label; }
    }
  });
}
