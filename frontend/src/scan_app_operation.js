import { ServingOperation } from "./core.js";

export function scanReadBody(request) {
  return { request_id: request.request_id, token: request.token };
}

export class ScanAppOperation {
  constructor(identifierFactory) {
    this.operation = new ServingOperation(identifierFactory);
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
  }

  async read(token, send) {
    if (this.inFlight) return null;
    const request = this.bind(token);
    const previouslySubmitted = this.readStarted;
    this.readStarted = true;
    return this.operation.submit(async body => {
      const result = await send(scanReadBody(body));
      return previouslySubmitted && result?.code === "INVALID_QR" ? { ...result, approved: false, code: "REQUEST_MISMATCH" } : result;
    }, request);
  }

  async retry(send) {
    if (!this.request?.token) throw new Error("Rescan the original QR to retry this serving.");
    return this.read(this.request.token, send);
  }

  reset() {
    if (this.request && !this.result || this.inFlight) throw new Error("Confirm the current serving before starting another meal.");
    this.operation.reset();
    this.readStarted = false;
  }
}
