import { escape } from "./core.js";
import { badge } from "./ui.js";

const emailLabels = {
  DRAFT: ["Draft, not sent", "neutral"],
  PENDING_APPROVAL: ["Waiting to send", "neutral"],
  QUEUED: ["Queued", "warning"],
  SENT: ["Sent", "positive"],
  FAILED: ["Failed", "danger"],
  NEEDS_REVIEW: ["Needs review", "danger"],
  CANCELLED: ["Cancelled", "neutral"]
};

export const SENDING_DISABLED = "Email sending is disabled. Drafts are kept; nothing has been sent.";

export function departmentEventLine(row) {
  return [row?.department_name, row?.event_name].filter(value => typeof value === "string" && value.trim()).join(" · ");
}

export function emailStatusBadge(row) {
  if (!row?.email) return badge("No email");
  if (!row.email_status) return badge("Not emailed");
  const [text, tone] = emailLabels[row.email_status] ?? ["Unknown", "neutral"];
  return badge(text, tone) + (row.email_code && row.email_status !== "SENT" ? `<span class="cell-subtitle">${escape(row.email_code.replaceAll("_", " ").toLowerCase())}</span>` : "");
}

export function emailActionLabel(row) {
  if (!row?.email) return null;
  if (row.email_status === "DRAFT" || !row.email_status) return "Email QR";
  if (["QUEUED", "PENDING_APPROVAL"].includes(row.email_status)) return null;
  return Number(row.resends_left ?? 0) > 0 ? `Resend QR (${row.resends_left} left)` : null;
}

export function confirmSendText(count, allowance) {
  const remaining = Number(allowance?.remaining);
  const limit = Number(allowance?.daily_limit);
  const tail = Number.isFinite(remaining) && Number.isFinite(limit) ? ` ${remaining} of today's ${limit} emails remain.` : "";
  return `Send ${count} email${count === 1 ? "" : "s"}?${tail}`;
}
