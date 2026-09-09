export function emailDeliverySettings(value) {
  if (!value || !["preview", "gmail", "ses"].includes(value.backend) || typeof value.sending_enabled !== "boolean" || typeof value.preview_available !== "boolean" || value.preview_available && value.backend !== "preview") throw new Error("Email delivery settings could not be confirmed. Refresh the queue to try again.");
  const automatic = value.automatic_enabled ?? false;
  const running = value.worker_running ?? false;
  const interval = value.poll_seconds ?? 5;
  const batch = value.batch_size ?? 10;
  if (typeof automatic !== "boolean" || typeof running !== "boolean" || !Number.isInteger(interval) || interval < 1 || interval > 300 || !Number.isInteger(batch) || batch < 1 || batch > 100 || automatic && (!value.sending_enabled || value.backend === "preview") || running && !automatic) throw new Error("Email delivery settings could not be confirmed. Refresh the queue to try again.");
  return { backend: value.backend, sending_enabled: value.sending_enabled, preview_available: value.preview_available, automatic_enabled: automatic, worker_running: running, poll_seconds: interval, batch_size: batch };
}

export function emailModeLabel(settings) {
  if (settings.backend === "preview") return "Local previews only; no emails sent.";
  if (settings.automatic_enabled) return `${settings.backend === "gmail" ? "Gmail" : "Amazon SES"} · automatic delivery ${settings.worker_running ? "running" : "not running"}.`;
  return `${settings.backend === "gmail" ? "Gmail" : "Amazon SES"} · sending ${settings.sending_enabled ? "enabled" : "disabled"}.`;
}

export function emailModeDescription(settings) {
  if (settings.backend === "preview") return "Preview mode does not deliver messages. Opening a preview leaves the message in the queue.";
  if (settings.automatic_enabled && settings.worker_running) return `Registration and QR resends queue emails for automatic delivery. The admin server checks the queue about every ${settings.poll_seconds ?? 5} seconds. Refresh this list to see updated delivery status.`;
  if (settings.automatic_enabled) return "Automatic delivery is configured, but its sender is not running. Queued emails will wait. Check that the admin server has started successfully.";
  if (!settings.sending_enabled) return "Email delivery is disabled. New registrations and QR resends remain queued until delivery is enabled.";
  return "Automatic delivery is off. Queued emails currently need an approved manual send using their queue ID.";
}

export function canPreviewEmail(row, settings) {
  return settings?.backend === "preview" && settings.preview_available === true && row.status === "QUEUED";
}

export function emailStatusDetail(status) {
  if (status === "SENT") return "Accepted by the email provider; inbox delivery is not confirmed.";
  if (status === "FAILED") return "Review this attempt before retrying.";
  if (status === "QUEUED") return "Waiting to be processed.";
  return "";
}
