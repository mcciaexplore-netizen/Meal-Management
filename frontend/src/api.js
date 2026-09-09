let csrfToken = null;
let unauthorized = () => {};

export function onUnauthorized(callback) {
  unauthorized = callback;
}

export function clearCredentials() {
  csrfToken = null;
}

export async function refreshCsrf() {
  const response = await fetch("/api/auth/csrf", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error("Unable to establish a secure session. Please reload and try again.");
  const data = await response.json();
  if (!data.csrf_token) throw new Error("Unable to establish a secure session. Please reload and try again.");
  csrfToken = data.csrf_token;
}

export async function api(path, options = {}) {
  const { method = "GET", body, responseType = "json" } = options;
  const headers = { Accept: responseType === "text" ? "text/html" : "application/json" };
  const mutation = !["GET", "HEAD"].includes(method);
  if (mutation) {
    if (!csrfToken) await refreshCsrf();
    headers["X-CSRF-Token"] = csrfToken;
  }
  let payload;
  if (body instanceof Blob) {
    headers["Content-Type"] = body.type;
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method, headers, body: payload, credentials: "same-origin", cache: "no-store"
    });
  } catch {
    throw new Error("The connection was interrupted. For a scan, retry this same serving to check its result.");
  }
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch {}
    if (response.status === 401 && path !== "/auth/login") unauthorized();
    const message = data.error?.message ?? (response.status === 429 ? "Too many attempts. Wait a moment before trying again." : "This request could not be completed. Please try again.");
    const error = new Error(message);
    error.status = response.status;
    error.code = data.error?.code;
    throw error;
  }
  if (response.status === 204) return null;
  if (responseType === "text") return response.text();
  if (responseType === "blob") return response.blob();
  return response.json();
}

export async function allPages(path) {
  const items = [];
  let cursor = null;
  do {
    const separator = path.includes("?") ? "&" : "?";
    const response = await api(`${path}${separator}limit=100${cursor ? `&after_id=${encodeURIComponent(cursor)}` : ""}`);
    items.push(...(response.items ?? []));
    if (response.next_cursor && response.next_cursor === cursor) throw new Error("The list could not be loaded. Please refresh and try again.");
    cursor = response.next_cursor;
  } while (cursor);
  return items;
}
