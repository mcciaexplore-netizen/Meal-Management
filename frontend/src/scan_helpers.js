import { positive } from "./core.js";

export function servingLocations(catalog) {
  return (catalog.locations ?? []).filter(row => row.is_active === true || row.is_active === 1);
}

export function servingScanners(catalog, locationId) {
  const locations = servingLocations(catalog);
  if (!locations.some(row => Number(row.id) === Number(locationId))) return [];
  return (catalog.scanners ?? []).filter(row => (row.is_active === true || row.is_active === 1) && Number(row.location_id) === Number(locationId));
}

export function scanDetails({ token, location_id, scanner_code, meal_type_id, category, authorization_id }, catalog, approvals, staffId) {
  const locationId = positive(location_id, "serving location");
  const scanner = servingScanners(catalog, locationId).find(row => row.code === scanner_code);
  if (!scanner) throw new Error("Choose an active registered counter at the selected location.");
  const mealTypeId = positive(meal_type_id, "meal type");
  if (!(catalog.meal_types ?? []).some(row => Number(row.id) === mealTypeId && (row.is_active === true || row.is_active === 1))) throw new Error("Choose an active meal type.");
  if (typeof token !== "string" || !token || token.trim() !== token || token.length > 256) throw new Error("Provide a valid QR credential without surrounding spaces.");
  const body = { token, scanner_code, meal_type_id: mealTypeId, quantity: 1 };
  if (category === "MASTER") {
    const approvalId = positive(authorization_id, "visitor approval");
    const approval = approvals.find(row => Number(row.authorization_id ?? row.id) === approvalId && Number(row.waiter_id) === Number(staffId));
    if (!approval) throw new Error("Choose a visitor approval assigned to your account.");
    if (Number(approval.location_id) !== locationId || approval.scanner_code !== scanner_code || Number(approval.meal_type_id) !== mealTypeId) throw new Error("Use the exact location, counter, and meal type specified in the visitor approval.");
    body.authorization_id = approvalId;
    body.request_id = approval.request_id;
    body.quantity = approval.quantity;
  } else if (category !== "EMPLOYEE") {
    throw new Error("Choose employee or authorized visitor meals.");
  }
  return body;
}

export function qrImageFile(file) {
  if (!file?.size) throw new Error("Choose a QR image first.");
  if (!["image/png", "image/jpeg", "image/webp", "image/svg+xml"].includes(file.type)) throw new Error("Choose a PNG, JPG, WebP, or SVG QR image.");
  if (file.size > 10 * 1024 * 1024) throw new Error("Choose a QR image smaller than 10 MB.");
  return file;
}

export async function approvedReceipt(result, load) {
  if (!result.approved || !result.request_id) return { receipt: null, unavailable: false };
  try {
    return { receipt: await load(result.request_id), unavailable: false };
  } catch {
    return { receipt: null, unavailable: true };
  }
}
