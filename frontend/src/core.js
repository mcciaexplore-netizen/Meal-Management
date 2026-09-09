export function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

export function dateInput(value = new Date()) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

export function dateRange(start, end) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end)) {
    throw new Error("Choose a valid start date and end date.");
  }
  const from = new Date(`${start}T00:00:00`);
  const until = new Date(`${end}T00:00:00`);
  if (!Number.isFinite(from.getTime()) || !Number.isFinite(until.getTime()) || dateInput(from) !== start || dateInput(until) !== end || from > until) {
    throw new Error("The end date must be on or after a valid start date.");
  }
  until.setDate(until.getDate() + 1);
  return { start: from.toISOString(), end: until.toISOString() };
}

export function timestamp(value) {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const parsed = new Date(normalized);
  if (!Number.isFinite(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

export function optionalExpiry(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime()) || parsed <= new Date()) throw new Error("Choose an expiry time in the future.");
  return parsed.toISOString();
}

export function positive(value, name) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) throw new Error(`Choose a valid ${name}.`);
  return parsed;
}

export function query(values) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "" && value !== null && value !== undefined)).toString();
}

export function list(data) {
  return Array.isArray(data) ? data : data?.items ?? [];
}

export function randomIdentifier(random = globalThis.crypto) {
  if (typeof random?.randomUUID === "function") return random.randomUUID();
  if (typeof random?.getRandomValues !== "function") throw new Error("This browser cannot create a secure serving identifier.");
  const bytes = random.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const value = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

export function isFinalScanResult(result, request) {
  if (!request || typeof result?.approved !== "boolean" || typeof result.code !== "string" || !result.code || result.request_id !== request.request_id || [
    "IDEMPOTENCY_KEY_REUSED", "REQUEST_MISMATCH", "PROCESSING_UNCONFIRMED", "SCAN_RECEIPT_UNCONFIRMED", "TRANSACTION_RETRY_REQUIRED", "VISITOR_DETAILS_REQUIRED"
  ].includes(result.code)) return false;
  if (!result.approved) return result.code !== "APPROVED";
  const positiveId = value => Number.isSafeInteger(value) && value > 0;
  return result.code === "APPROVED" && positiveId(result.serving_id) && Array.isArray(result.meal_ids) && result.meal_ids.length === (request.quantity ?? 1) && result.meal_ids.length > 0 && result.meal_ids.every(positiveId) && new Set(result.meal_ids).size === result.meal_ids.length;
}

export class ServingOperation {
  constructor(createIdentifier = () => randomIdentifier()) {
    this.createIdentifier = createIdentifier;
    this.reset();
  }

  reset() {
    this.request = null;
    this.result = null;
    this.inFlight = false;
  }

  bind(details) {
    if (this.request) {
      if (!this.request.token && details.token) this.request = Object.freeze({ ...this.request, token: details.token });
      return { ...this.request };
    }
    this.request = Object.freeze({ ...details, request_id: details.request_id || this.createIdentifier() });
    return { ...this.request };
  }

  restore(details) {
    if (this.request || this.inFlight) throw new Error("A serving is already in progress.");
    this.request = Object.freeze({ ...details });
  }

  forgetCredential() {
    if (!this.request) return;
    const { token, ...details } = this.request;
    this.request = Object.freeze(details);
  }

  accept(result) {
    if (!isFinalScanResult(result, this.request)) return false;
    this.result = result;
    return true;
  }

  async submit(send, details) {
    if (this.inFlight) return null;
    if (this.result) return { ...this.result, duplicate: true };
    this.inFlight = true;
    try {
      const result = await send(this.bind(details));
      this.accept(result);
      return result;
    } finally {
      this.inFlight = false;
    }
  }
}
