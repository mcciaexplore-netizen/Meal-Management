const identifiers = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const activeKey = "meal-office:public-scanner-active";
const scopedKey = /^meal-office:public-scanner:([0-9a-f]{64})$/;

function storageKey(scope) {
  if (typeof scope !== "string" || !/^[0-9a-f]{64}$/.test(scope)) throw new Error("Invalid scanner scope.");
  return `meal-office:public-scanner:${scope}`;
}

function safeRequest(request) {
  if (!identifiers.test(request?.request_id ?? "") || request.quantity !== 1) throw new Error("Invalid serving marker.");
  return { request_id: request.request_id, quantity: 1 };
}

export function saveScannerMarker(storage, scope, request) {
  let entries;
  try {
    const key = storageKey(scope);
    const safe = safeRequest(request);
    entries = [[activeKey, JSON.stringify({ scope, request: safe })], [key, JSON.stringify(safe)]];
  } catch {
    return false;
  }
  let saved = false;
  for (const [key, value] of entries) {
    try { storage.setItem(key, value); saved = true; } catch {}
  }
  return saved;
}

export function loadScannerMarker(storage, scope) {
  try {
    const raw = storage.getItem(storageKey(scope));
    return raw ? safeRequest(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}

function activeMarker(storage) {
  try {
    const marker = JSON.parse(storage.getItem(activeKey));
    storageKey(marker.scope);
    return { scope: marker.scope, request: safeRequest(marker.request) };
  } catch {
    return null;
  }
}

export function loadActiveScannerMarker(storage, scope) {
  let marker = activeMarker(storage);
  if (marker && marker.scope !== scope) return marker;
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const match = scopedKey.exec(storage.key(index) ?? "");
      if (!match) continue;
      const request = loadScannerMarker(storage, match[1]);
      if (!request) continue;
      const candidate = { scope: match[1], request };
      if (candidate.scope !== scope) return candidate;
      marker ??= candidate;
    }
  } catch {}
  return marker;
}

export function clearScannerMarker(storage, scope) {
  try {
    storage.removeItem(storageKey(scope));
    if (activeMarker(storage)?.scope === scope) storage.removeItem(activeKey);
    return true;
  } catch {
    return false;
  }
}
