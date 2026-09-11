function changedScope() {
  const error = new Error("This browser's scanner session has changed. The previous serving cannot be confirmed here. Ask the office administrator to check it before serving another meal.");
  error.code = "SCANNER_SCOPE_CHANGED";
  return error;
}

export class ScannerTransport {
  constructor(fetcher = (...args) => fetch(...args)) {
    this.fetcher = fetcher;
    this.session = null;
    this.refreshNeeded = false;
    this.connecting = null;
  }

  get scope() { return this.session?.scope ?? null; }

  acceptSession(session) {
    if (!/^[0-9a-f]{64}$/.test(session?.scope ?? "") || !/^[0-9a-f]{64}$/.test(session?.csrf_token ?? "")) throw new Error("The scanner could not establish a secure browser session. Please try again.");
    this.session = { scope: session.scope, csrf_token: session.csrf_token };
    this.refreshNeeded = false;
    return { scope: session.scope };
  }

  async response(path, options) {
    let response;
    try { response = await this.fetcher(`/api/scanner${path}`, { credentials: "same-origin", cache: "no-store", ...options }); }
    catch {
      this.refreshNeeded = true;
      throw new Error("The connection was interrupted. Retry this same serving to confirm its result before serving another meal.");
    }
    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch {}
      const fallback = response.status === 429 ? "Too many scans. Wait a moment, then retry the same serving." : "The scanner is unavailable. Please try again or contact the office administrator.";
      const error = new Error(data.error?.message ?? fallback);
      error.status = response.status;
      error.code = data.error?.code;
      throw error;
    }
    try { return await response.json(); }
    catch { throw new Error("The scanner response could not be confirmed. Retry the same serving."); }
  }

  async connect() {
    if (this.connecting) return this.connecting;
    this.connecting = (async () => {
      const session = await this.response("/session", { method: "GET", headers: { Accept: "application/json" } });
      return this.acceptSession(session);
    })();
    try { return await this.connecting; }
    finally { this.connecting = null; }
  }

  async activate(activationCode) {
    const session = await this.response("/activate", {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ activation_code: activationCode })
    });
    return this.acceptSession(session);
  }

  async request(path, { method = "GET", body } = {}, expectedScope = this.scope) {
    if (!this.session || this.refreshNeeded) await this.connect();
    if (expectedScope && this.scope !== expectedScope) throw changedScope();
    const send = async () => {
      const headers = { Accept: "application/json" };
      const options = { method, headers };
      if (method !== "GET") headers["X-CSRF-Token"] = this.session.csrf_token;
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      return this.response(path, options);
    };
    try { return await send(); }
    catch (error) {
      if (error.status !== 401 && !(error.status === 403 && error.code === "CSRF_REJECTED")) throw error;
      const originalScope = expectedScope ?? this.scope;
      await this.connect();
      if (this.scope !== originalScope) throw changedScope();
      return send();
    }
  }
}
