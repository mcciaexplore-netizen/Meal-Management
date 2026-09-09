import { ServingOperation } from "./core.js";

export function scanReadBody(request) {
  return { request_id: request.request_id, token: request.token };
}

export function scanVisitorBody(request) {
  return { ...scanReadBody(request), visitor_details: request.visitor_details };
}

export function masterDetailsRequested(result, request) {
  return Boolean(request) && result?.request_id === request.request_id && result.kind === "MASTER" && result.next === "VISITOR_DETAILS" && result.approved !== true;
}

export function visitorFields(values) {
  const limits = { company_name: 150, name: 150, email: 254, phone: 32 };
  const fields = {};
  for (const [name, maximum] of Object.entries(limits)) {
    if (typeof values[name] !== "string") throw new Error("Complete Company Name, Name, Email, and Phone.");
    const value = values[name].trim();
    if (!value || value.length > maximum) throw new Error("Complete Company Name, Name, Email, and Phone using valid lengths.");
    fields[name] = value;
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(fields.email)) throw new Error("Enter a valid visitor email address.");
  fields.email = fields.email.toLowerCase();
  const digits = fields.phone.replace(/[^0-9]/g, "").length;
  if (!/^\+?[0-9 ()\-.]+$/.test(fields.phone) || digits < 7 || digits > 15) throw new Error("Enter a phone number with 7 to 15 digits.");
  return Object.freeze(fields);
}

export class ScanAppOperation {
  constructor(identifierFactory) {
    this.operation = new ServingOperation(identifierFactory);
    this.needsVisitor = false;
    this.readStarted = false;
  }

  get request() { return this.operation.request; }
  get result() { return this.operation.result; }
  get inFlight() { return this.operation.inFlight; }

  restore(request) {
    if (request.quantity !== 1 || Object.keys(request).some(key => !["request_id", "quantity"].includes(key))) throw new Error("The previous serving belongs to a different scanner workflow.");
    this.operation.restore({ request_id: request.request_id, quantity: 1 });
    this.readStarted = true;
  }

  bind(token) {
    if (typeof token !== "string" || !token || token.trim() !== token || token.length > 256) throw new Error("Scan a valid QR image.");
    return this.operation.bind({ token, quantity: 1 });
  }

  accept(result) {
    return this.operation.accept(result);
  }

  forgetCredential() {
    this.operation.forgetCredential();
    if (this.request?.visitor_details) {
      const { visitor_details, ...request } = this.request;
      this.operation.request = Object.freeze(request);
    }
    this.needsVisitor = false;
  }

  async read(token, send) {
    if (this.inFlight) return null;
    const request = this.bind(token);
    const previouslySubmitted = this.readStarted;
    this.readStarted = true;
    const response = await this.operation.submit(async body => {
      const result = await send(scanReadBody(body));
      return previouslySubmitted && result?.code === "INVALID_QR" ? { ...result, approved: false, code: "REQUEST_MISMATCH" } : result;
    }, request);
    if (masterDetailsRequested(response, this.request)) this.needsVisitor = true;
    return response;
  }

  bindVisitor(values) {
    if (!this.needsVisitor || !this.request) throw new Error("Scan the master QR before entering visitor details.");
    const fields = visitorFields(values);
    if (this.request.visitor_details) {
      if (JSON.stringify(this.request.visitor_details) !== JSON.stringify(fields)) throw new Error("These visitor details are bound to the pending serving. Retry the original request.");
      return this.request;
    }
    this.operation.request = Object.freeze({ ...this.request, visitor_details: fields });
    return this.request;
  }

  async recordVisitor(values, send) {
    const request = this.bindVisitor(values);
    return this.operation.submit(body => send(scanVisitorBody(body)), request);
  }

  correctVisitor() {
    if (!this.needsVisitor || this.result || this.inFlight || !this.request?.visitor_details) throw new Error("Visitor details cannot be changed for this serving.");
    const { visitor_details, ...request } = this.request;
    this.operation.request = Object.freeze(request);
    return visitor_details;
  }

  async retry(sendRead, sendRecord) {
    if (!this.request?.token) throw new Error("Rescan the original QR to retry this serving.");
    return this.request.visitor_details ? this.operation.submit(body => sendRecord(scanVisitorBody(body)), this.request) : this.read(this.request.token, sendRead);
  }

  reset() {
    if (this.request && !this.result || this.inFlight) throw new Error("Confirm the current serving before starting another meal.");
    this.operation.reset();
    this.needsVisitor = false;
    this.readStarted = false;
  }
}
