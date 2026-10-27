import { api } from "./api.js";
import { escape, timestamp } from "./core.js";
import { badge, closeModal, empty, errorMessage, field, formAction, heading, icon, modal, notify, table } from "./ui.js";

const IST_OFFSET_MINUTES = 330;
const MAX_ACTIVE_MONTHS = 6;
const sources = {
  CODE: ["Code", "positive"],
  SECRET: ["Emergency code", "warning"],
  LEGACY: ["Before codes", "neutral"]
};

export function formatScannerCode(code) {
  const value = String(code ?? "").replace(/[\s-]/g, "").toUpperCase();
  return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4)}` : value;
}

export function istDate(now = new Date()) {
  return new Date(now.getTime() + IST_OFFSET_MINUTES * 60000).toISOString().slice(0, 10);
}

export function addMonths(day, months) {
  const [year, month, date] = day.split("-").map(Number);
  const total = month - 1 + months;
  const targetYear = year + Math.floor(total / 12);
  const targetMonth = (total % 12 + 12) % 12;
  const last = new Date(Date.UTC(targetYear, targetMonth + 1, 0)).getUTCDate();
  return `${String(targetYear).padStart(4, "0")}-${String(targetMonth + 1).padStart(2, "0")}-${String(Math.min(date, last)).padStart(2, "0")}`;
}

export function activeDateRange(now = new Date()) {
  const today = istDate(now);
  return { min: today, max: addMonths(today, MAX_ACTIVE_MONTHS) };
}

export function scannerCodePayload(values, now = new Date()) {
  const label = String(values.label ?? "").trim();
  const activeUntil = String(values.active_until ?? "");
  if (!label || label.length > 100) throw new Error("Enter a label of up to 100 characters, such as the waiter's name or the counter.");
  const range = activeDateRange(now);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(activeUntil) || activeUntil < range.min || activeUntil > range.max) {
    throw new Error("Choose an active-until date from today up to six months ahead.");
  }
  return { label, active_until: activeUntil };
}

export function displayActiveUntil(row) {
  if (!row.active_until_date) return "Until revoked";
  const [year, month, day] = row.active_until_date.split("-").map(Number);
  const formatted = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, day)));
  return `End of ${formatted} (IST)`;
}

function sourceBadge(source) {
  const [text, tone] = sources[source] ?? [source, "neutral"];
  return badge(text, tone);
}

function revokeButton(row, text) {
  return `<button type="button" class="small-button" data-revoke="${escape(row.id)}" data-label="${escape(row.label)}" data-kind="${row.activated_at ? "phone" : "code"}">${escape(text)}</button>`;
}

function showCode(result, reload) {
  const dialog = modal("Scanner activation code", `<p class="muted">Read this code to the waiter, or type it on the phone's <strong>Activate this scanner</strong> screen. It is shown only once.</p><p class="scanner-code mono" aria-label="Activation code">${escape(formatScannerCode(result.code))}</p><dl class="scanner-code-details"><div><dt>For</dt><dd>${escape(result.label)}</dd></div><div><dt>Use before</dt><dd>${escape(timestamp(result.code_expires_at))}</dd></div><div><dt>Phone works until</dt><dd>${escape(displayActiveUntil(result))}</dd></div></dl><div class="form-actions"><button type="button" class="button" id="copy-scanner-code">Copy code</button><button type="button" class="button primary" id="close-scanner-code">Done</button></div>`);
  dialog.querySelector("#copy-scanner-code").onclick = async () => {
    try {
      await navigator.clipboard.writeText(formatScannerCode(result.code));
      notify("Code copied.");
    } catch {
      notify("Copy is unavailable in this browser. Read the code from the screen.", "error");
    }
  };
  const finish = async () => { closeModal(); await reload(); };
  dialog.querySelector("#close-scanner-code").onclick = finish;
  dialog.querySelector("[data-close-modal]").onclick = finish;
}

export const LABEL_NOTE = "The name is shown as the Waiter on meal records and rejected scans, and it cannot be edited later. Name each phone after its vendor or counter when you generate its code.";

function generateDialog(range, reload) {
  const dialog = modal("Generate scanner code", `<form>${field("Vendor or counter name", "label", "", "text", 'required maxlength="100" placeholder="For example: Annapurna Caterers, Counter 2" autocomplete="off"')}<p class="field-help">${escape(LABEL_NOTE)}</p>${field("Active until", "active_until", range.max, "date", `required min="${escape(range.min)}" max="${escape(range.max)}"`)}<p class="field-help">The phone stops working at the end of this day, India time (IST). Choose up to six months ahead. The code itself must be used within 24 hours.</p><div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">Generate code</button></div></form>`);
  formAction(dialog.querySelector("form"), async data => {
    const result = await api("/scanner-phones", { method: "POST", body: scannerCodePayload(Object.fromEntries(data)) });
    showCode(result, reload);
  });
}

export async function scannerAccess(target, context) {
  target.innerHTML = `${heading("SCANNER ACCESS", "Scanner phones", "Give a waiter's phone access to the Meal Scanner with a one-time code, and revoke it at any time.", `<button class="button primary" id="generate-scanner-code">${icon("plus")} Generate code</button>`)}<section class="panel"><div class="panel-heading"><div><h2>Active scanner phones</h2><p class="muted small">Revoking stops a phone on its next request.</p><p class="muted small label-note">${escape(LABEL_NOTE)}</p></div></div><div id="scanner-phones"></div></section><section class="panel"><div class="panel-heading"><div><h2>Unused codes</h2><p class="muted small">Codes expire if not used within 24 hours.</p></div></div><div id="scanner-codes"></div></section>`;
  async function load() {
    const data = await api("/scanner-phones");
    if (!context.isCurrent()) return;
    const range = { min: data.today ?? activeDateRange().min, max: data.latest_active_date ?? activeDateRange().max };
    target.querySelector("#generate-scanner-code").onclick = () => generateDialog(range, load);
    const phones = data.phones ?? [];
    const codes = data.codes ?? [];
    target.querySelector("#scanner-phones").innerHTML = phones.length ? table(["Vendor or counter", "Source", "Activated", "Active until", ""], phones.map(row => `<tr><td class="label-cell"><strong>${escape(row.label)}</strong></td><td>${sourceBadge(row.source)}</td><td>${escape(timestamp(row.activated_at))}</td><td>${escape(displayActiveUntil(row))}</td><td>${revokeButton(row, "Revoke")}</td></tr>`), "Active scanner phones") : empty("No active scanner phones", "Generate a code and enter it on a waiter's phone to activate it.", "scan");
    target.querySelector("#scanner-codes").innerHTML = codes.length ? table(["Vendor or counter", "Use before", "Phone works until", ""], codes.map(row => `<tr><td class="label-cell"><strong>${escape(row.label)}</strong></td><td>${escape(timestamp(row.code_expires_at))}</td><td>${escape(displayActiveUntil(row))}</td><td>${revokeButton(row, "Cancel")}</td></tr>`), "Unused scanner codes") : empty("No unused codes", "New codes appear here until a phone uses them.", "scan");
    target.querySelectorAll("[data-revoke]").forEach(button => button.onclick = () => {
      const phone = button.dataset.kind === "phone";
      const dialog = modal(phone ? "Revoke scanner phone" : "Cancel scanner code", `<p>${phone ? `Stop the scanner phone <strong>${escape(button.dataset.label)}</strong> now? It will need a new code to scan again.` : `Cancel the unused code for <strong>${escape(button.dataset.label)}</strong>?`}</p><form><div data-form-error></div><div class="form-actions"><button type="submit" class="button danger-outline">${phone ? "Revoke phone" : "Cancel code"}</button></div></form>`);
      formAction(dialog.querySelector("form"), async () => {
        await api(`/scanner-phones/${encodeURIComponent(button.dataset.revoke)}/revoke`, { method: "POST" });
        closeModal();
        notify(phone ? "Scanner phone revoked." : "Code cancelled.");
        await load();
      });
    });
  }
  try {
    await load();
  } catch (error) {
    if (context.isCurrent()) target.querySelector("#scanner-phones").innerHTML = errorMessage(error);
  }
}
