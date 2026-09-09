import { api } from "./api.js";
import { escape, positive } from "./core.js";
import { badge, empty, heading, icon, modal, notify } from "./ui.js";

export function developmentAccess(user, catalog) {
  return user?.roles?.includes("ADMIN") === true && catalog?.development_test_data === true;
}

export function scenarioLabel(employee) {
  if (employee.expected_code === "APPROVED") return { title: "Active employee", expected: "Expected: meal approved", tone: "positive" };
  const labels = {
    QR_EXPIRED: "Expired QR",
    QR_REVOKED: "Revoked QR",
    EMPLOYEE_INACTIVE: "Inactive employee"
  };
  return { title: labels[employee.expected_code] ?? String(employee.scenario ?? "Rejection scenario").replaceAll("_", " "), expected: `Intentional rejection: ${employee.expected_code ?? "not approved"}`, tone: "warning" };
}

export async function developmentGallery(target, context) {
  if (!developmentAccess(context.user, context.catalog)) throw new Error("Development QR access is unavailable.");
  const data = await api("/development/test-data");
  if (!context.isCurrent()) return;
  const employees = data.employees ?? [];
  const master = data.master;
  const rows = employees.map(employee => ({ ...employee, label: employee.full_name, ...scenarioLabel(employee) }));
  if (master) rows.push({ ...master, label: master.label ?? "Development office master QR", title: "Master QR", expected: "Enter Company Name, Name, Email, and Phone to record one visitor meal", tone: "neutral" });
  target.innerHTML = `${heading("DEVELOPMENT ONLY", "Test QR gallery", "Private development credentials for checking employee and visitor meal scanning.")}<div class="info-banner">${icon("qr")}<span>Expired, revoked, and inactive cases are intentional test scenarios. These records use the same scan API and meal ledger as other credentials. Keep downloaded QR files private.</span></div>${rows.length ? `<div class="test-qr-grid">${rows.map(row => `<article class="panel test-qr-card"><div class="test-qr-card-header">${icon("qr")}${badge(row.title, row.tone)}</div><h2>${escape(row.label)}</h2>${row.employee_code ? `<p class="mono">${escape(row.employee_code)}</p>` : ""}<p class="muted small">${escape(row.expected)}</p><div class="test-qr-actions"><button class="button primary" type="button" data-view-test-qr="${escape(row.qr_id)}">View QR</button><button class="button" type="button" data-download-test-qr="${escape(row.qr_id)}">Download SVG</button></div></article>`).join("")}</div>` : `<section class="panel">${empty("No development records yet", "The explicit development data setup must complete before test QRs appear here.", "qr")}</section>`}${data.waiters?.length ? `<section class="panel padded"><h2>Development helper accounts</h2><p class="muted small">Helper accounts use waiter permissions. Their passwords are set separately.</p><ul class="catalog-list">${data.waiters.map(waiter => `<li><span><strong>${escape(waiter.display_name)}</strong><small>${escape(waiter.email)}</small></span>${badge("Waiter")}</li>`).join("")}</ul></section>` : ""}`;
  let disposed = false;
  const urls = new Set();
  function release(url) {
    URL.revokeObjectURL(url);
    urls.delete(url);
  }
  async function load(button, download) {
    const id = positive(download ? button.dataset.downloadTestQr : button.dataset.viewTestQr, "QR");
    const row = rows.find(item => Number(item.qr_id) === id);
    if (!row || disposed) return;
    button.disabled = true;
    try {
      const image = await api(`/development/test-data/qrs/${id}`, { responseType: "blob" });
      if (disposed || !context.isCurrent()) return;
      if (!image.type.toLowerCase().startsWith("image/svg+xml")) throw new Error("The QR image could not be retrieved.");
      const url = URL.createObjectURL(image);
      urls.add(url);
      if (download) {
        const link = document.createElement("a");
        link.href = url;
        link.download = `meal-office-development-qr-${id}.svg`;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => release(url), 1000);
      } else {
        const dialog = modal(row.label, `${badge(row.title, row.tone)}<p class="muted small">${escape(row.expected)}</p><div class="qr-card"><img class="qr-image" src="${escape(url)}" alt="Development QR for ${escape(row.label)}"></div><p class="field-help">This is a private development QR credential. Scan it using the serving station or download its SVG for image-upload testing.</p>`);
        dialog.addEventListener("close", () => release(url), { once: true });
      }
    } catch (error) {
      if (!disposed) notify(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }
  target.querySelectorAll("[data-view-test-qr]").forEach(button => button.onclick = () => load(button, false));
  target.querySelectorAll("[data-download-test-qr]").forEach(button => button.onclick = () => load(button, true));
  return () => { disposed = true; for (const url of urls) release(url); };
}
