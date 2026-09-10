export function scannerUrl(configuration) {
  let address;
  try { address = new URL(configuration?.scanner_url); }
  catch { throw new Error("The Meal Scanner address is not configured correctly."); }
  if (!["http:", "https:"].includes(address.protocol) || address.username || address.password || address.pathname !== "/" || address.search || address.hash) {
    throw new Error("The Meal Scanner address is not configured correctly.");
  }
  return address.origin;
}

export async function loadApplication(request) {
  const configuration = await request("/application");
  return { scanner_url: scannerUrl(configuration), max_photo_bytes: employeePhotoLimit(configuration.max_photo_bytes) };
}
import { employeePhotoLimit } from "./employee_photo.js";
