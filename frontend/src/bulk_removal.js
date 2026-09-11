export function bulkRemovalPayload(kind, values, reason) {
  const key = kind === "employees" ? "employee_ids" : kind === "meals" ? "meal_ids" : null;
  if (!key) throw new Error("Choose a supported record type.");
  if (values.some(value => typeof value === "boolean" || typeof value === "string" && !/^[1-9][0-9]*$/.test(value))) {
    throw new Error("The selected records are invalid. Refresh and try again.");
  }
  const ids = values.map(value => Number(value));
  if (!ids.length) throw new Error("Select at least one record.");
  if (ids.length > 100) throw new Error("Select no more than 100 records at a time.");
  if (ids.some(id => !Number.isSafeInteger(id) || id < 1) || new Set(ids).size !== ids.length) {
    throw new Error("The selected records are invalid. Refresh and try again.");
  }
  const normalizedReason = String(reason ?? "").trim();
  if (!normalizedReason || normalizedReason.length > 255 || Array.from(normalizedReason).some(character => character.charCodeAt(0) < 32)) {
    throw new Error("Enter a removal reason of up to 255 characters.");
  }
  return { [key]: ids.sort((left, right) => left - right), reason: normalizedReason };
}
