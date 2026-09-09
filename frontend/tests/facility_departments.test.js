import assert from "node:assert/strict";
import test from "node:test";
import { employeeDepartmentOptions, resolveEmployeeDepartment } from "../src/facility_departments.js";

const selection = "facility:auditorium";
const row = { id: 42, name: "Auditorium", is_active: true };

test("facility choices reuse persisted identities and retain unrelated departments", () => {
  const original = [{ ...row, name: "AUDITORIUM" }, { id: 9, name: "Existing Office", is_active: true }];
  const choices = employeeDepartmentOptions(original);
  assert.deepEqual(choices.find(item => item.name === "Auditorium"), row);
  assert.equal(choices.filter(item => item.name.toLowerCase() === "auditorium").length, 1);
  assert.equal(choices.find(item => item.name === "Existing Office").id, 9);
  assert.equal(original[0].name, "AUDITORIUM");
  assert.equal(choices.find(item => item.name === "Rubber & Polymer Testing Lab").id, "facility:rubber-polymer");
});

test("inactive departments are not offered again as unsaved facility choices", () => {
  const choices = employeeDepartmentOptions([{ ...row, is_active: false }]);
  assert.ok(!choices.some(item => item.name === "Auditorium" || item.id === selection));
});

test("selecting an existing department does not create or reload catalog data", async () => {
  const unexpected = async () => { throw new Error("Unexpected catalog request"); };
  assert.equal(await resolveEmployeeDepartment("42", { request: unexpected, refreshCatalog: unexpected }), 42);
});

test("an unsaved facility selection first reuses a department another admin already created", async () => {
  assert.equal(await resolveEmployeeDepartment(selection, {
    request: async () => { throw new Error("Unexpected creation"); },
    refreshCatalog: async () => ({ departments: [row] })
  }), 42);
});

test("a missing facility is created through the existing API and its confirmed ID is returned", async () => {
  let refreshes = 0;
  const calls = [];
  const identifier = await resolveEmployeeDepartment(selection, {
    refreshCatalog: async () => ({ departments: refreshes++ ? [row] : [] }),
    request: async (...args) => { calls.push(args); return { id: 42 }; }
  });
  assert.equal(identifier, 42);
  assert.deepEqual(calls, [["/catalog/departments", { method: "POST", body: { name: "Auditorium" } }]]);
  assert.equal(refreshes, 2);
});

test("concurrent department creation recovers the existing ID without overwriting it", async () => {
  let refreshes = 0;
  let writes = 0;
  assert.equal(await resolveEmployeeDepartment(selection, {
    refreshCatalog: async () => ({ departments: refreshes++ ? [row] : [] }),
    request: async () => { writes += 1; throw Object.assign(new Error("Already exists"), { code: "DEPARTMENT_NAME_EXISTS" }); }
  }), 42);
  assert.equal(writes, 1);
});

test("a lost creation response does not create another department on the next registration attempt", async () => {
  let saved = false;
  let writes = 0;
  const dependencies = {
    refreshCatalog: async () => ({ departments: saved ? [row] : [] }),
    request: async () => { saved = true; writes += 1; throw new Error("Connection interrupted"); }
  };
  await assert.rejects(resolveEmployeeDepartment(selection, dependencies), /Connection interrupted/);
  assert.equal(await resolveEmployeeDepartment(selection, dependencies), 42);
  assert.equal(writes, 1);
});

test("inactive or unconfirmed catalog records cannot be used for registration", async () => {
  for (const departments of [[{ ...row, is_active: false }], [{ ...row, is_active: 0 }]]) {
    await assert.rejects(resolveEmployeeDepartment(selection, {
      refreshCatalog: async () => ({ departments }),
      request: async () => { throw new Error("Must not reactivate"); }
    }), /inactive/);
  }
  await assert.rejects(resolveEmployeeDepartment(selection, {
    refreshCatalog: async () => ({ departments: [] }),
    request: async () => ({ id: 42 })
  }), /could not be confirmed/);
});

test("catalog permissions and service failures propagate before employee registration", async () => {
  for (const code of ["ROLE_REQUIRED", "AUTHENTICATION_REQUIRED", "SERVICE_UNAVAILABLE"]) {
    await assert.rejects(resolveEmployeeDepartment(selection, {
      refreshCatalog: async () => ({ departments: [] }),
      request: async () => { throw Object.assign(new Error("Request failed"), { code }); }
    }), error => error.code === code);
  }
});

test("invalid selections and malformed catalog responses cannot produce guessed IDs", async () => {
  const unexpected = async () => { throw new Error("Unexpected request"); };
  for (const value of ["facility:administrator", "facility:99", "0", "-1", "", "42;DELETE", "1.5"]) {
    await assert.rejects(resolveEmployeeDepartment(value, { request: unexpected, refreshCatalog: unexpected }));
  }
  await assert.rejects(resolveEmployeeDepartment(selection, {
    request: unexpected,
    refreshCatalog: async () => ({})
  }), /could not be loaded/);
  await assert.rejects(resolveEmployeeDepartment(selection, {
    request: async () => ({ id: "not-an-id" }),
    refreshCatalog: async () => ({ departments: [] })
  }));
});
