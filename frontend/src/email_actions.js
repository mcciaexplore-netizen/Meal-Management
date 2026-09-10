import { canApproveEmail, canProcessEmail, emailProcessBatchSize } from "./email_delivery.js";

const outcomes = new Set(["SENT", "ALREADY_SENT", "FAILED", "CANCELLED", "NEEDS_REVIEW"]);
const identifier = value => Number.isSafeInteger(value) && value > 0;

export function validateEmailResult(result, emailId) {
  if (!result || result.email_id !== emailId || !outcomes.has(result.status)) throw new Error("The email result is not confirmed. Check its status before trying again.");
  return result;
}

export class SingleEmailOperation {
  constructor(employeeId, row, request) {
    this.employeeId = employeeId;
    this.emailId = row.id ?? row.email_id;
    if (!identifier(employeeId) || !identifier(this.emailId)) throw new Error("Choose a valid employee email.");
    this.request = request;
    this.status = row.status;
    this.inFlight = false;
    this.unknown = false;
  }

  async send() {
    if (this.inFlight) return null;
    if (this.unknown) throw new Error("Check this email's status before retrying the same message.");
    if (!["DRAFT", "QUEUED"].includes(this.status)) throw new Error("Only an individual draft or approved unsent email can be sent. Refresh its status first.");
    this.inFlight = true;
    this.unknown = true;
    try {
      const result = validateEmailResult(await this.request(`/employees/${this.employeeId}/emails/${this.emailId}/send`, { method: "POST" }), this.emailId);
      this.status = result.status;
      this.unknown = false;
      return result;
    } catch {
      throw new Error("The email result is not confirmed. Check its status before retrying this same email. Do not create another resend.");
    } finally {
      this.inFlight = false;
    }
  }

  async check() {
    if (this.inFlight) return null;
    this.inFlight = true;
    try {
      const row = await this.request(`/employees/${this.employeeId}/emails/${this.emailId}`);
      if ((row?.id ?? row?.email_id) !== this.emailId || !["DRAFT", "QUEUED", ...outcomes].includes(row.status)) throw new Error("The email status is not confirmed. Try Check status again.");
      this.status = row.status;
      this.unknown = false;
      return row;
    } finally {
      this.inFlight = false;
    }
  }
}

export function selectedEmailIds(rows, selected, mode) {
  const eligible = mode === "approve" ? canApproveEmail : mode === "process" ? canProcessEmail : null;
  if (!eligible) throw new Error("Choose a valid email action.");
  if (!Array.isArray(selected) || !selected.length || selected.length > 100 || new Set(selected).size !== selected.length || selected.some(id => !identifier(id))) throw new Error("Select between 1 and 100 distinct messages.");
  const lookup = new Map(rows.map(row => [row.id ?? row.email_id, row]));
  if (selected.some(id => !lookup.has(id) || !eligible(lookup.get(id)))) throw new Error(mode === "approve" ? "Select only bulk messages awaiting approval." : "Select only approved bulk messages waiting to be processed.");
  return [...selected].sort((a, b) => a - b);
}

export class BulkEmailOperation {
  constructor(request, processBatchSize = 10) {
    this.request = request;
    this.processBatchSize = emailProcessBatchSize(processBatchSize);
    this.inFlight = false;
    this.ids = [];
    this.results = [];
    this.unknown = false;
    this.cancelled = false;
  }

  setProcessBatchSize(value) {
    if (this.inFlight) throw new Error("Wait for the current email operation to finish before updating its settings.");
    this.processBatchSize = emailProcessBatchSize(value);
  }

  cancel() {
    this.cancelled = true;
  }

  async approve(rows, selected) {
    if (this.inFlight) return null;
    if (this.unknown) throw new Error("Refresh the selected messages before retrying approval.");
    const ids = selectedEmailIds(rows, selected, "approve");
    this.inFlight = true;
    this.unknown = true;
    try {
      const result = await this.request("/email-queue/approve", { method: "POST", body: { email_ids: ids } });
      if (!Array.isArray(result?.email_ids) || JSON.stringify([...result.email_ids].sort((a, b) => a - b)) !== JSON.stringify(ids)) throw new Error("Approval is not confirmed. Refresh the queue before processing.");
      this.unknown = false;
      return result;
    } finally {
      this.inFlight = false;
    }
  }

  async approveAndProcess(rows, selected, progress = () => {}) {
    if (this.inFlight) return null;
    const ids = selectedEmailIds(rows, selected, "approve");
    const approval = await this.approve(rows, ids);
    if (!approval) return null;
    const approved = rows.filter(row => ids.includes(row.id ?? row.email_id)).map(row => ({ ...row, status: "QUEUED", approved_at: true }));
    progress([], ids.length);
    const results = await this.process(approved, ids, progress);
    return { email_ids: ids, results };
  }

  async process(rows, selected, progress = () => {}) {
    if (this.inFlight) return null;
    if (this.unknown) throw new Error("Refresh the selected messages before processing again.");
    const ids = selectedEmailIds(rows, selected, "process");
    const batchSize = emailProcessBatchSize(this.processBatchSize);
    this.inFlight = true;
    this.ids = ids;
    this.results = [];
    this.unknown = false;
    try {
      for (let index = 0; index < ids.length; index += batchSize) {
        if (this.cancelled) break;
        const chunk = ids.slice(index, index + batchSize);
        this.unknown = true;
        const response = await this.request("/email-queue/process", { method: "POST", body: { email_ids: chunk } });
        if (!Array.isArray(response?.results) || response.results.length !== chunk.length || new Set(response.results.map(row => row.email_id)).size !== chunk.length) throw new Error("The processing result is not confirmed. Refresh these messages before processing again.");
        const results = response.results.map(result => {
          if (!chunk.includes(result.email_id)) throw new Error("The processing result is not confirmed. Refresh these messages before processing again.");
          return validateEmailResult(result, result.email_id);
        });
        this.results.push(...results);
        this.unknown = false;
        progress([...this.results], ids.length);
        if (results.some(result => ["FAILED", "NEEDS_REVIEW"].includes(result.status))) break;
      }
      return [...this.results];
    } catch {
      throw new Error("Processing stopped because a result is not confirmed. Refresh the selected messages before retrying; do not approve or create replacement emails.");
    } finally {
      this.inFlight = false;
    }
  }
}
