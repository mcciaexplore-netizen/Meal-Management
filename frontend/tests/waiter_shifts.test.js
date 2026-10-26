import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { NOTICE_VERSION, SHIFT_NOTICE, WARNING_MINUTES, locationError, requestPosition, shiftEndTime, shiftState, signInForm, signInPayload } from "../src/shift_signin.js";
import { accuracyLabel, canteenPayload, mapLink } from "../src/waiter_shifts.js";

const now = new Date("2026-10-26T06:00:00Z");
const position = { coords: { latitude: 18.52043, longitude: 73.856743, accuracy: 1800 } };
const valid = { name: "  Ravi  ", consent: true, selfie: "YWJj", position };

test("the shift warning shows in the last ten minutes and the shift ends at expiry", () => {
  assert.equal(WARNING_MINUTES, 10);
  assert.deepEqual(shiftState({ expires_at: "2026-10-26T09:00:00" }, now), { active: true, warning: false, minutesLeft: 180, remainingMs: 3 * 3600000 });
  const warning = shiftState({ expires_at: "2026-10-26T06:10:00Z" }, now);
  assert.equal(warning.warning, true);
  assert.equal(warning.minutesLeft, 10);
  assert.equal(shiftState({ expires_at: "2026-10-26T06:11:00Z" }, now).warning, false);
  assert.equal(shiftState({ expires_at: "2026-10-26T06:00:00Z" }, now).active, false);
  assert.equal(shiftState({ expires_at: "not a date" }, now).active, false);
  assert.equal(shiftState(null, now).active, false);
});

test("the end time is shown in India time", () => {
  assert.equal(shiftEndTime({ expires_at: "2026-10-26T09:00:00" }), "14:30");
  assert.equal(shiftEndTime(null), "");
});

test("sign-in needs a name, the I agree tick, a selfie and a location, and keeps poor accuracy", () => {
  assert.deepEqual(signInPayload(valid), {
    waiter_name: "Ravi", latitude: 18.52043, longitude: 73.856743, accuracy_m: 1800,
    consent: true, notice_version: NOTICE_VERSION, selfie: "YWJj"
  });
  assert.throws(() => signInPayload({ ...valid, name: " " }), /Enter your name/);
  assert.throws(() => signInPayload({ ...valid, consent: false }), /I agree/);
  assert.throws(() => signInPayload({ ...valid, consent: "on" }), /I agree/);
  assert.throws(() => signInPayload({ ...valid, selfie: "" }), /selfie/);
  assert.throws(() => signInPayload({ ...valid, selfie: "data:image/jpeg;base64,YWJj" }), /selfie/);
  assert.throws(() => signInPayload({ ...valid, position: null }), /location/);
  assert.throws(() => signInPayload({ ...valid, position: { coords: { latitude: 1, longitude: Number.NaN, accuracy: 5 } } }), /location/);
});

test("a refused location explains how to allow it", async () => {
  assert.match(locationError({ code: 1 }), /permission is needed/);
  assert.match(locationError({ code: 3 }), /took too long/);
  assert.match(locationError({ code: 2 }), /unavailable/);
  await assert.rejects(requestPosition(undefined), error => error.code === 2);
  await assert.rejects(requestPosition({ getCurrentPosition: (_, reject) => reject({ code: 1 }) }), error => error.code === 1);
  let options;
  const found = await requestPosition({ getCurrentPosition: (resolve, _, chosen) => { options = chosen; resolve(position); } });
  assert.equal(found, position);
  assert.deepEqual(options, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
});

test("the sign-in form shows the notice, an I agree box and a front-camera selfie", () => {
  const html = signInForm('Ravi <script>');
  assert.ok(html.includes(SHIFT_NOTICE.slice(0, 40)));
  assert.match(SHIFT_NOTICE, /90 days/);
  assert.match(html, /type="checkbox" name="consent" required/);
  assert.match(html, /I agree/);
  assert.match(html, /capture="user"/);
  assert.match(html, /value="Ravi &lt;script&gt;"/);
});

test("the scanner signs in, warns, offers sign in again and resets the meal type for a new shift", async () => {
  const source = await readFile(new URL("../src/scan_app.js", import.meta.url), "utf8");
  assert.match(source, /if \(needsSignIn\(\)\) renderSignIn\(\);\s*else renderScanner\(\);/);
  assert.match(source, /id="shift-banner"[^>]*hidden/);
  assert.match(source, />Sign in again</);
  assert.match(source, /id="shift-sign-out">Sign out</);
  assert.match(source, /"\/shift\/end", \{ method: "POST" \}/);
  assert.match(source, /error\.code === "SHIFT_SIGN_IN_REQUIRED"/);
  const resets = source.match(/currentShift = (?:shift|null);\s*selectedMealType = null;/g) ?? [];
  assert.ok(resets.length >= 3, "every new or ended shift clears the meal type");
});

test("admin shift helpers link the map, round accuracy and validate the canteen location", () => {
  assert.equal(mapLink(18.5204304, 73.8567431), "https://www.openstreetmap.org/?mlat=18.520430&mlon=73.856743#map=18/18.520430/73.856743");
  assert.equal(accuracyLabel(12.6), "±13 m");
  assert.equal(accuracyLabel(null), "—");
  assert.deepEqual(canteenPayload({ latitude: "18.5", longitude: "73.8", radius_m: "100" }), { latitude: 18.5, longitude: 73.8, radius_m: 100 });
  assert.deepEqual(canteenPayload({ latitude: "", longitude: " ", radius_m: "" }), { latitude: null, longitude: null, radius_m: null });
  assert.throws(() => canteenPayload({ latitude: "18.5", longitude: "", radius_m: "100" }), /together/);
  assert.throws(() => canteenPayload({ latitude: "95", longitude: "73.8", radius_m: "100" }), /Latitude/);
  assert.throws(() => canteenPayload({ latitude: "18.5", longitude: "73.8", radius_m: "5" }), /Radius/);
  assert.throws(() => canteenPayload({ latitude: "18.5", longitude: "73.8", radius_m: "100.5" }), /Radius/);
});

test("the admin page offers End shift now and hides expired selfies", async () => {
  const source = await readFile(new URL("../src/waiter_shifts.js", import.meta.url), "utf8");
  assert.match(source, />End shift now</);
  assert.match(source, /\/waiter-shifts\/\$\{encodeURIComponent\(button\.dataset\.endShift\)\}\/end/);
  assert.match(source, /Selfie expired/);
  const app = await readFile(new URL("../src/app.js", import.meta.url), "utf8");
  assert.match(app, /\["waiter-shifts", \["Waiter shifts"/);
});
