import { api } from "./api.js";
import { dateRange, positive, query } from "./core.js";

export function exportFileName(start, end) {
  return `meal-report_${start}_to_${end}.xlsx`;
}

export function exportPath(data) {
  const start = data.get("start");
  const end = data.get("end");
  dateRange(start, end);
  const optional = name => data.get(name) ? positive(data.get(name), name.replace("_id", "").replace("_", " ")) : null;
  return `/reports/meals/export?${query({ from: start, through: end, employee_id: optional("employee_id"), department_id: optional("department_id"), meal_type_id: optional("meal_type_id") })}`;
}

export async function downloadMealReport(form, button, errors, request = api) {
  if (button.disabled) return;
  const label = button.textContent;
  errors.innerHTML = "";
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const data = new FormData(form);
    const blob = await request(exportPath(data), { responseType: "blob" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = exportFileName(data.get("start"), data.get("end"));
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  } catch (error) {
    const message = document.createElement("div");
    message.className = "alert error";
    message.setAttribute("role", "alert");
    message.textContent = error?.status ? error.message : error?.message?.startsWith("The connection was interrupted") ? "The connection was interrupted. Export again." : error?.message || "The report could not be prepared. Try again.";
    errors.replaceChildren(message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}
