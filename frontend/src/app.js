import { api, clearCredentials, onUnauthorized, refreshCsrf } from "./api.js";
import { escape } from "./core.js";
import { closeModal, errorMessage, formAction, icon, loading, notify } from "./ui.js";
import { dashboard, employees, masters, reports, emails, settings } from "./screens.js";
import { developmentGallery } from "./development.js";
import { loadApplication } from "./application.js";

const root = document.getElementById("app");
const adminPages = new Map([
  ["dashboard", ["Overview", "grid", dashboard]],
  ["employees", ["Employees", "people", employees]],
  ["master", ["Master QR", "qr", masters]],
  ["reports", ["Meal history", "chart", reports]],
  ["emails", ["Email queue", "mail", emails]],
  ["settings", ["Office settings", "settings", settings]]
]);
let user = null;
let catalog = null;
let application = null;
let disposeScreen = () => {};
let navigationVersion = 0;

function pageList() {
  const pages = new Map(user.roles.includes("ADMIN") ? adminPages : []);
  if (user.roles.includes("ADMIN") && catalog?.development_test_data === true) pages.set("test-data", ["Development QRs", "qr", developmentGallery]);
  return pages;
}

function login(message = "") {
  navigationVersion += 1;
  disposeScreen();
  disposeScreen = () => {};
  closeModal();
  clearCredentials();
  user = null;
  catalog = null;
  application = null;
  root.innerHTML = `<main id="main" class="login-layout"><section class="login-story"><a class="brand" href="#">${icon("plate")}<span>Meal Office<span>STAFF WORKSPACE</span></span></a><div><span class="label-pill">ONE WORKSPACE. EVERY SERVING.</span><h1>A smoother<br>meal service.</h1><p>Manage employee meals, welcome visitors, and keep an accurate record of every serving.</p><div class="login-rule"></div><p class="login-small">Personal employee QRs. Master QR visitor meals.<br>One clear record.</p></div><span class="login-footer">Designed for your everyday operations</span></section><section class="login-form-wrap"><div class="login-form"><div class="mobile-brand">${icon("plate")} Meal Office</div><p class="eyebrow">WELCOME BACK</p><h2>Sign in to your workspace</h2><p class="muted">Use your staff account to continue.</p><form id="login-form"><label>Email address<input type="email" name="email" required maxlength="254" autocomplete="username" autofocus></label><label>Password<input type="password" name="password" required maxlength="1024" autocomplete="current-password"></label><div data-form-error>${message ? `<div class="alert">${escape(message)}</div>` : ""}</div><button class="button primary full" type="submit">Sign in ${icon("arrow")}</button></form><p class="login-help">Need access? Contact your office administrator.</p></div></section></main>`;
  formAction(document.getElementById("login-form"), async data => {
    user = await api("/auth/login", { method: "POST", body: { email: data.get("email"), password: data.get("password") } });
    await refreshCsrf();
    await openWorkspace();
  });
}

async function openWorkspace() {
  const version = ++navigationVersion;
  try {
    application = await loadApplication(api);
    if (!user || version !== navigationVersion) return;
    if (!user.roles.includes("ADMIN") && user.roles.includes("WAITER")) { window.location.replace(application.scanner_url); return; }
    catalog = await api("/catalog");
  } catch (error) {
    if (!user || version !== navigationVersion) return;
    root.innerHTML = `<main id="main" class="startup"><h1>Unable to open workspace</h1>${errorMessage(error)}<button class="button primary" id="retry-workspace">Try again</button><button class="button" id="exit-workspace">Sign out</button></main>`;
    document.getElementById("retry-workspace").onclick = openWorkspace;
    document.getElementById("exit-workspace").onclick = logout;
    return;
  }
  if (version !== navigationVersion || !user) return;
  const pages = pageList();
  const initials = user.display_name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join("");
  root.innerHTML = `<div class="workspace"><aside class="sidebar"><a class="brand" href="#${pages.keys().next().value}">${icon("plate")}<span>Meal Office<span>STAFF WORKSPACE</span></span></a><div class="workspace-caption">WORKSPACE</div><nav aria-label="Main navigation">${[...pages].map(([key, [label, name]]) => `<a href="#${key}" data-page="${key}">${icon(name)}<span>${label}</span></a>`).join("")}<a href="${escape(application.scanner_url)}">${icon("scan")}<span>Open Meal Scanner</span></a></nav><div class="sidebar-bottom"><span class="avatar">${escape(initials)}</span><div><strong>${escape(user.display_name)}</strong><span>${user.roles.includes("ADMIN") ? "Administrator" : "Waiter"}</span></div><button class="icon-button" id="logout" aria-label="Sign out">${icon("logout")}</button></div></aside><div class="workspace-main"><header class="topbar"><span class="topbar-label">Office meal management</span><span class="date-label">${escape(new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(new Date()))}</span></header><main id="main" tabindex="-1"></main><footer class="page-footer">Meal Office <span>Times shown in ${escape(Intl.DateTimeFormat().resolvedOptions().timeZone)}</span></footer></div></div>`;
  document.getElementById("logout").onclick = logout;
  await navigate();
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
    login();
  } catch (error) {
    notify(error.message, "error");
  }
}

async function navigate() {
  if (!user || !document.querySelector(".workspace")) return;
  if (location.hash.slice(1).split("?")[0] === "scan") { window.location.replace(application.scanner_url); return; }
  disposeScreen();
  disposeScreen = () => {};
  closeModal();
  const pages = pageList();
  const key = location.hash.slice(1).split("?")[0];
  const selected = pages.has(key) ? key : pages.keys().next().value;
  if (!selected) { login("Your account does not have a workspace role. Contact an administrator."); return; }
  document.querySelectorAll("[data-page]").forEach(link => {
    const active = link.dataset.page === selected;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const target = document.getElementById("main");
  target.innerHTML = loading();
  const version = ++navigationVersion;
  const context = {
    user,
    catalog,
    scanner_url: application.scanner_url,
    isCurrent: () => version === navigationVersion,
    reload: navigate,
    refreshCatalog: async () => { catalog = await api("/catalog"); context.catalog = catalog; return catalog; }
  };
  try {
    const dispose = await pages.get(selected)[2](target, context);
    if (version !== navigationVersion) { if (dispose) dispose(); return; }
    disposeScreen = dispose ?? (() => {});
    document.title = `${pages.get(selected)[0]} · Meal Office`;
  } catch (error) {
    if (version !== navigationVersion) return;
    target.innerHTML = `${errorMessage(error)}<button class="button" id="reload-page">Try again</button>`;
    document.getElementById("reload-page").onclick = navigate;
  }
}

window.addEventListener("hashchange", navigate);
onUnauthorized(() => login("Your session has ended. Sign in again to continue."));

try {
  user = await api("/auth/me");
  await refreshCsrf();
  await openWorkspace();
} catch (error) {
  if (error.status === 401) login();
  else login(error.message);
}
