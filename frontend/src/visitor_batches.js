import { api } from "./api.js";
import { escape, list, optionalExpiry, query, timestamp } from "./core.js";
import { employeeDepartmentOptions, resolveEmployeeDepartment } from "./facility_departments.js";
import { mealTypeCheckboxes, selectedMealTypeIds } from "./meal_types.js";
import { badge, closeModal, empty, errorMessage, field, formAction, heading, icon, loading, modal, notify, selectField, table } from "./ui.js";
import { SENDING_DISABLED, confirmSendText, departmentEventLine, emailActionLabel, emailStatusBadge } from "./visitor_qrs.js";

export const MAX_FILE_BYTES = 1_048_576;
export const COLUMNS = ["Sr. No.", "Person Name", "Company Name", "Phone", "Email"];
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const qrLabels = { ACTIVE: ["Active", "positive"], USED_UP: ["Used up", "neutral"], EXPIRED: ["Expired", "warning"], REVOKED: ["Revoked", "danger"] };

export function uploadType(file) {
  const name = String(file?.name ?? "").toLowerCase();
  if (name.endsWith(".csv")) return "text/csv";
  if (name.endsWith(".xlsx")) return XLSX;
  throw new Error("Choose a .csv or .xlsx file.");
}

export function checkFile(file) {
  if (!file) throw new Error("Choose a CSV or Excel (.xlsx) file.");
  const type = uploadType(file);
  if (file.size > MAX_FILE_BYTES) throw new Error("The file is larger than 1 MB.");
  if (file.size === 0) throw new Error("The file is empty.");
  return type;
}

export function requiredExpiry(value) {
  const expiry = optionalExpiry(value);
  if (!expiry) throw new Error("Set an expiry for the visitor QRs.");
  return expiry;
}

export function batchSummary(batch) {
  const emails = [["emails_pending", "to send"], ["emails_queued", "queued"], ["emails_sent", "sent"], ["emails_failed", "failed"]]
    .filter(([key]) => Number(batch[key]) > 0).map(([key, label]) => `${batch[key]} ${label}`);
  return emails.length ? emails.join(" · ") : "No emails";
}

export function errorRows(errors) {
  return errors.map(item => `<tr><td class="mono">${escape(item.row)}</td><td>${escape(item.column)}</td><td>${escape(item.message)}</td></tr>`);
}

function previewTable(rows) {
  return table(["Row", ...COLUMNS], rows.map(row => `<tr><td class="mono">${escape(row.row)}</td><td>${escape(row.sr_no)}</td><td>${escape(row.person_name)}</td><td>${escape(row.company_name)}</td><td class="nowrap">${escape(row.phone)}</td><td>${escape(row.email || "—")}</td></tr>`), "Visitor rows");
}

function requestKey(file) {
  return `visitor-batch:${file.name}:${file.size}:${file.lastModified}`;
}

function requestId(file) {
  let value = null;
  try { value = window.sessionStorage.getItem(requestKey(file)); } catch {}
  if (!value) {
    value = crypto.randomUUID();
    try { window.sessionStorage.setItem(requestKey(file), value); } catch {}
  }
  return value;
}

export async function visitorBatches(target, context) {
  let emailSettings = null;
  try { emailSettings = await api("/email-settings"); } catch { emailSettings = null; }
  const sending = emailSettings?.sending_enabled === true;
  target.innerHTML = `${heading("VISITOR MEAL ACCESS", "Bulk visitor QRs", "Create one visitor QR per person from a CSV or Excel list. Every row is checked before anything is created.")}<section class="panel padded"><h2>Upload a visitor list</h2><p class="muted small">Columns, in this order: ${escape(COLUMNS.join(", "))}. Up to 200 people and 1 MB. Email is optional; people without one get no email.</p><form id="visitor-batch-form"><div class="form-grid"><label>Visitor list<input type="file" name="file" accept=".csv,.xlsx,text/csv,${XLSX}" required></label>${field("Expiry, local time", "expires_at", "", "datetime-local", "required")}${selectField("Department", "department_id", employeeDepartmentOptions(context.catalog?.departments ?? []), "")}${field("Event name (optional)", "event_name", "", "text", 'maxlength="150"')}</div>${mealTypeCheckboxes(context.catalog?.meal_types ?? [], null, "Each person can have one meal of each ticked type.")}<div data-form-error></div><div class="form-actions"><button type="submit" class="button primary">Check list</button></div></form><div id="visitor-batch-preview"></div></section><section class="panel"><div class="panel-heading"><div><h2>Batches</h2><p class="muted small" id="email-allowance"></p></div></div><div id="visitor-batch-list">${loading()}</div><div class="pagination" id="visitor-batch-pagination"></div></section>`;
  const form = target.querySelector("#visitor-batch-form");
  const preview = target.querySelector("#visitor-batch-preview");
  formAction(form, async data => {
    const file = form.elements.file.files[0];
    const type = checkFile(file);
    const settings = {
      expires_at: requiredExpiry(data.get("expires_at")),
      meal_type_ids: selectedMealTypeIds(data.getAll("meal_type_ids"), "Tick at least one meal type."),
      event_name: String(data.get("event_name") ?? "").trim() || null,
      department_id: await resolveEmployeeDepartment(data.get("department_id"), { request: api, refreshCatalog: context.refreshCatalog })
    };
    const result = await api(`/visitor-batches/preview?${query({ filename: file.name })}`, { method: "POST", body: new Blob([file], { type }) });
    if (!context.isCurrent()) return;
    if (!result.valid) {
      preview.innerHTML = `<div class="alert error" role="alert"><strong>${escape(result.errors.length)} problem${result.errors.length === 1 ? "" : "s"} found. Nothing was created.</strong> Correct the file and check it again.</div>${table(["Row", "Column", "Problem"], errorRows(result.errors), "Problems")}`;
      return;
    }
    preview.innerHTML = `<p><strong>${escape(result.row_count)} visitors</strong>, ${escape(result.email_count)} with an email address.</p>${previewTable(result.rows)}<div class="form-actions"><button type="button" class="button primary" id="create-visitor-batch">Create ${escape(result.row_count)} visitor QRs</button></div><div data-create-error></div>`;
    preview.querySelector("#create-visitor-batch").onclick = async event => {
      event.currentTarget.disabled = true;
      try {
        const created = await api("/visitor-batches", { method: "POST", body: { request_id: requestId(file), source_name: file.name, rows: result.rows, ...settings } });
        notify(created.duplicate ? `This list was already created as batch ${created.batch_id}.` : `Batch ${created.batch_id} created.`);
        preview.innerHTML = "";
        form.reset();
        await load();
        await detail(created.batch_id);
      } catch (error) {
        preview.querySelector("[data-create-error]").innerHTML = errorMessage(error);
        event.currentTarget.disabled = false;
      }
    };
  });
  let rows = [];
  let cursor = null;
  let allowance = null;
  async function load(append = false) {
    const result = await api(`/visitor-batches?${query({ limit: 20, before: append ? cursor : null })}`);
    if (!context.isCurrent()) return;
    rows = append ? rows.concat(list(result)) : list(result);
    cursor = result.next_cursor;
    allowance = result.email_allowance ?? allowance;
    target.querySelector("#email-allowance").textContent = sending
      ? (allowance ? `Email allowance: ${allowance.remaining} of ${allowance.daily_limit} left in the last 24 hours.` : "")
      : SENDING_DISABLED;
    target.querySelector("#visitor-batch-list").innerHTML = rows.length ? table(["Batch", "Created", "Department · event", "Visitors", "Used up", "Emails", ""], rows.map(batch => `<tr><td class="mono">${escape(batch.id)}<span class="cell-subtitle">${escape(batch.source_name)}</span></td><td class="nowrap">${escape(timestamp(batch.created_at))}<span class="cell-subtitle">by ${escape(batch.created_by)}</span></td><td>${escape(departmentEventLine(batch))}</td><td>${escape(batch.row_count)}</td><td>${escape(batch.used_up)}</td><td>${escape(batchSummary(batch))}</td><td><button type="button" class="small-button" data-batch="${escape(batch.id)}">View</button></td></tr>`), "Visitor batches") : empty("No batches yet", "Upload a visitor list to create individual visitor QRs.", "qr");
    target.querySelector("#visitor-batch-pagination").innerHTML = `<span>${rows.length} batches shown</span>${cursor ? '<button class="button" id="more-batches">Load more</button>' : ""}`;
    target.querySelector("#more-batches")?.addEventListener("click", () => load(true).catch(error => notify(error.message, "error")));
    target.querySelectorAll("[data-batch]").forEach(button => button.onclick = () => detail(Number(button.dataset.batch)));
  }
  async function processApproved(emailIds) {
    const size = Number(emailSettings?.process_batch_size) || 10;
    let sent = 0;
    for (let index = 0; index < emailIds.length; index += size) {
      const result = await api("/email-queue/process", { method: "POST", body: { email_ids: emailIds.slice(index, index + size) } });
      sent += (result.results ?? []).filter(item => item.status === "SENT").length;
      if ((result.results ?? []).some(item => item.code === "EMAIL_DAILY_LIMIT_REACHED")) break;
    }
    return sent;
  }
  async function detail(batchId) {
    const dialog = modal(`Visitor batch ${batchId}`, loading(), true);
    try {
      const batch = await api(`/visitor-batches/${batchId}`);
      if (!dialog.open) return;
      const pending = batch.people.filter(person => person.email_status === "PENDING_APPROVAL").length;
      dialog.querySelector(".modal-content").innerHTML = `<div class="detail-grid"><div><span>Department</span><strong>${escape(batch.department_name)}</strong></div><div><span>Event</span><strong>${escape(batch.event_name ?? "—")}</strong></div><div><span>Valid until</span><strong>${escape(timestamp(batch.expires_at))}</strong></div><div><span>Visitors</span><strong>${escape(batch.row_count)}</strong></div></div><div class="stack-actions" id="batch-send">${pending ? `<button type="button" class="button primary" id="send-batch" ${sending ? "" : "disabled"}>Send emails (${pending})</button>` : ""}${sending ? "" : `<p class="field-help">${escape(SENDING_DISABLED)}</p>`}</div>${table(["Sr. No.", "Person", "Phone", "QR", "Email", ""], batch.people.map(person => `<tr><td class="mono">${escape(person.sr_no)}</td><td><strong>${escape(person.person_name)}</strong><span class="cell-subtitle">${escape(person.company_name)}</span></td><td class="nowrap">${escape(person.phone)}</td><td>${badge(...(qrLabels[person.qr_status] ?? ["Unknown", "neutral"]))}</td><td>${escape(person.email ?? "—")}<span class="cell-subtitle">${emailStatusBadge(person)}</span></td><td>${emailActionLabel(person) && person.email_status !== "PENDING_APPROVAL" && person.qr_status === "ACTIVE" ? `<button type="button" class="small-button" data-resend="${escape(person.qr_id)}" ${sending ? "" : "disabled"}>${escape(emailActionLabel(person))}</button>` : ""}</td></tr>`), "Visitors in this batch")}`;
      dialog.querySelector("#send-batch")?.addEventListener("click", event => {
        const holder = document.createElement("div");
        holder.className = "inline-confirm";
        holder.innerHTML = `<p>${escape(confirmSendText(pending, allowance))}</p><div class="form-actions"><button type="button" class="button" data-cancel>Cancel</button><button type="button" class="button primary" data-confirm>Send ${escape(pending)} emails</button></div><div data-form-error></div>`;
        event.currentTarget.replaceWith(holder);
        holder.querySelector("[data-cancel]").onclick = () => detail(batchId);
        holder.querySelector("[data-confirm]").onclick = async () => {
          holder.querySelector("[data-confirm]").disabled = true;
          try {
            const approved = await api(`/visitor-batches/${batchId}/send-emails`, { method: "POST", body: { expected_count: pending } });
            const sent = await processApproved(approved.email_ids ?? []);
            notify(`${sent} of ${approved.approved_count} emails sent.`);
            await load();
            await detail(batchId);
          } catch (error) {
            holder.querySelector("[data-form-error]").innerHTML = errorMessage(error);
            holder.querySelector("[data-confirm]").disabled = false;
          }
        };
      });
      dialog.querySelectorAll("[data-resend]").forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {
          const result = await api(`/master-qrs/${button.dataset.resend}/email`, { method: "POST", body: { confirm: true } });
          notify(result.status === "SENT" ? "Email sent." : `Email ${String(result.status).toLowerCase().replaceAll("_", " ")}.`);
          await detail(batchId);
        } catch (error) {
          notify(error.message, "error");
          button.disabled = false;
        }
      });
    } catch (error) { if (dialog.open) dialog.querySelector(".modal-content").innerHTML = errorMessage(error); }
  }
  await load();
  return () => closeModal();
}
