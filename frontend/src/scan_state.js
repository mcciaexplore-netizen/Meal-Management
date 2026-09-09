const identifiers = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function storageKey(staffId) {
  if (!Number.isSafeInteger(Number(staffId)) || Number(staffId) < 1) throw new Error("Invalid staff identity.");
  return `meal-office:serving:${Number(staffId)}`;
}

function safeRequest(request) {
  if (!identifiers.test(request.request_id ?? "") || typeof request.scanner_code !== "string" || !request.scanner_code.length || request.scanner_code.length > 64 || !Number.isSafeInteger(request.meal_type_id) || request.meal_type_id < 1 || !Number.isSafeInteger(request.quantity) || request.quantity < 1) throw new Error("Invalid serving marker.");
  const safe = { request_id: request.request_id, scanner_code: request.scanner_code, meal_type_id: request.meal_type_id, quantity: request.quantity };
  if (request.authorization_id !== undefined && request.authorization_id !== null) {
    if (!Number.isSafeInteger(request.authorization_id) || request.authorization_id < 1) throw new Error("Invalid serving marker.");
    safe.authorization_id = request.authorization_id;
  }
  return safe;
}

export function saveServingMarker(storage, staffId, request, locationId) {
  try {
    const marker = { request: safeRequest(request), location_id: Number(locationId) };
    if (!Number.isSafeInteger(marker.location_id) || marker.location_id < 1) return false;
    storage.setItem(storageKey(staffId), JSON.stringify(marker));
    return true;
  } catch {
    return false;
  }
}

export function loadServingMarker(storage, staffId) {
  try {
    const raw = storage.getItem(storageKey(staffId));
    if (!raw) return null;
    const marker = JSON.parse(raw);
    if (!Number.isSafeInteger(marker.location_id) || marker.location_id < 1) return null;
    return { request: safeRequest(marker.request), location_id: marker.location_id };
  } catch {
    return null;
  }
}

export function clearServingMarker(storage, staffId) {
  try {
    storage.removeItem(storageKey(staffId));
    return true;
  } catch {
    return false;
  }
}
