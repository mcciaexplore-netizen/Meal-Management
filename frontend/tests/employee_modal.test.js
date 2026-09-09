import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { employeeDetail } from "../src/screens.js";
import { modal } from "../src/ui.js";

const originalDocument = globalThis.document;
const originalFetch = globalThis.fetch;

afterEach(() => {
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
  globalThis.fetch = originalFetch;
});

function fixture() {
  const pending = [];
  const closeButton = {};
  let content;
  const dialog = {
    open: false,
    set innerHTML(value) { content = { innerHTML: value }; },
    querySelector(selector) { return selector === ".modal-content" ? content : closeButton; },
    setAttribute() {},
    showModal() { this.open = true; },
  };
  globalThis.document = { getElementById: () => dialog };
  globalThis.fetch = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
  return { dialog, pending, content: () => content };
}

test("delayed employee details cannot overwrite a newly opened registration dialog", async () => {
  const view = fixture();
  const waiting = employeeDetail(7, { isCurrent: () => true }, async () => {});
  const earlier = view.content();
  modal("Register an employee", '<section class="registration-photo">Camera is active</section>');
  const registration = view.content();
  const initial = registration.innerHTML;
  assert.notEqual(registration, earlier);
  for (const pending of view.pending) pending.resolve(Response.json({ items: [] }));
  await waiting;
  assert.equal(view.content(), registration);
  assert.equal(registration.innerHTML, initial);
});

test("a delayed employee details failure cannot replace a newer registration camera with an error", async () => {
  const view = fixture();
  const waiting = employeeDetail(7, { isCurrent: () => true }, async () => {});
  modal("Register an employee", '<section class="registration-photo">Camera is active</section>');
  const registration = view.content();
  const initial = registration.innerHTML;
  view.pending[0].reject(new Error("Employee lookup failed"));
  for (const pending of view.pending.slice(1)) pending.resolve(Response.json({ items: [] }));
  await waiting;
  assert.equal(view.dialog.open, true);
  assert.equal(registration.innerHTML, initial);
});
