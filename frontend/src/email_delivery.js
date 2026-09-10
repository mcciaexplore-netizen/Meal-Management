export function emailDeliverySettings(value) {
  if (value && value.approval_required !== true) throw new Error("Email approval settings could not be confirmed. Ask the administrator to update the admin application and apply the email approval migration before sending.");
  if (!value || !["preview", "gmail", "ses"].includes(value.backend) || typeof value.sending_enabled !== "boolean" || typeof value.preview_available !== "boolean" || value.preview_available && value.backend !== "preview" || value.approval_required !== true) throw new Error("Email delivery settings could not be confirmed. Refresh to try again.");
  const automatic = value.automatic_enabled ?? false;
  const running = value.worker_running ?? false;
  const interval = value.poll_seconds ?? 5;
  const batch = value.batch_size ?? 10;
  if (typeof automatic !== "boolean" || typeof running !== "boolean" || !Number.isInteger(interval) || interval < 1 || interval > 300 || !Number.isInteger(batch) || batch < 1 || batch > 100 || automatic && (!value.sending_enabled || value.backend === "preview") || running && !automatic) throw new Error("Email delivery settings could not be confirmed. Refresh to try again.");
  return { backend: value.backend, sending_enabled: value.sending_enabled, preview_available: value.preview_available, approval_required: true, automatic_enabled: automatic, worker_running: running, poll_seconds: interval, batch_size: batch };
}

export function emailModeLabel(settings) {
  if (settings.backend === "preview") return "Local previews only; no emails sent.";
  return `${settings.backend === "gmail" ? "Gmail" : "Amazon SES"} · sending ${settings.sending_enabled ? "enabled" : "disabled"}.`;
}

export function emailModeDescription(settings) {
  if (settings.backend === "preview") return "Preview mode does not deliver messages. Ask the administrator to configure real email delivery to enable Send email and bulk processing. Previewing does not approve or send a message.";
  if (!settings.sending_enabled) return "Email delivery is disabled. Ask the administrator to enable email sending. Individual drafts need Send email; bulk emails need approval before processing.";
  if (settings.automatic_enabled && settings.worker_running) return `Individual drafts send only when you choose Send email. Bulk emails require approval; the sender checks approved bulk messages about every ${settings.poll_seconds ?? 5} seconds. Refresh to see updated status.`;
  if (settings.automatic_enabled) return "Individual drafts need Send email. Bulk emails require approval. The background sender is not running; use Process approved to send selected approved messages.";
  return "Individual drafts send only when you choose Send email. Choose Approve and send for selected bulk messages. Use Process approved to resume an interrupted approved selection.";
}

export function canSendEmail(settings) {
  return settings?.sending_enabled === true && ["gmail", "ses"].includes(settings.backend);
}

export function canPreviewEmail(row, settings) {
  return settings?.backend === "preview" && settings.preview_available === true && ["DRAFT", "PENDING_APPROVAL", "QUEUED"].includes(row.status);
}

export function canApproveEmail(row) {
  return ["BULK", "LEGACY"].includes(row.delivery_mode) && row.status === "PENDING_APPROVAL";
}

export function canProcessEmail(row) {
  return ["BULK", "LEGACY"].includes(row.delivery_mode) && row.status === "QUEUED" && Boolean(row.approved_at);
}

export function emailStatusDetail(status) {
  if (["SENT", "ALREADY_SENT"].includes(status)) return "Accepted by the email provider; inbox delivery is not confirmed.";
  if (status === "NEEDS_REVIEW") return "Delivery may have occurred. Review this attempt before creating another email.";
  if (status === "FAILED") return "Review this failed attempt before creating another email.";
  if (status === "CANCELLED") return "This message will not be sent. Check the employee and QR status.";
  if (status === "DRAFT") return "Not sent. Choose Send email to send this individual message.";
  if (status === "PENDING_APPROVAL") return "Not sent. Waiting for explicit administrator approval.";
  if (status === "QUEUED") return "Approved and waiting to be processed.";
  return "";
}
