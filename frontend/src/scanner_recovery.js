const identifiers = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function requestMarker(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 2 || !Object.hasOwn(value, "request_id") || !Object.hasOwn(value, "quantity") || value.quantity !== 1 || typeof value.request_id !== "string" || !identifiers.test(value.request_id) || value.request_id.replaceAll("-", "") === "0".repeat(32)) {
    throw new Error("The previous serving could not be verified. Retry startup before scanning another meal.");
  }
  return { request_id: value.request_id.toLowerCase(), quantity: 1 };
}

export function recoveryMarker(response) {
  if (!response || typeof response !== "object" || Array.isArray(response) || Object.keys(response).length !== 1 || !Object.hasOwn(response, "request")) {
    throw new Error("The previous serving could not be verified. Retry startup before scanning another meal.");
  }
  return response.request === null ? null : requestMarker(response.request);
}

export async function chooseScannerRecovery({ localMarker, currentRequest, recover }) {
  if (currentRequest) return { request: null, fromServer: false };
  if (localMarker) return { request: requestMarker(localMarker), fromServer: false };
  const request = recoveryMarker(await recover());
  return { request, fromServer: request !== null };
}
