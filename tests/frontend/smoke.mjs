// Browser smoke test of the Network panel and cards against the harness.
//
//   npm install --no-save playwright && npx playwright install chromium
//   node tests/frontend/smoke.mjs
//
// Serves the repository, opens tests/frontend/harness.html, clicks through the
// panel and checks the actions sent, that nothing throws, and that the page
// never scrolls sideways on a phone.

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const types = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json" };
const server = createServer(async (req, res) => {
  const path = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname)).replace(/^([/\\])+/, "");
  const file = join(root, path);
  if (!file.startsWith(root)) return res.writeHead(403).end();
  try {
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": types[extname(file)] || "application/octet-stream" }).end(body);
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const base = `http://127.0.0.1:${server.address().port}/tests/frontend/harness.html`;

const failures = [];
const check = (cond, message) => {
  if (!cond) failures.push(message);
};

const browser = await chromium.launch();
async function open(query, viewport = { width: 1280, height: 900 }) {
  const page = await browser.newPage({ viewport });
  page.on("pageerror", (err) => failures.push(`${query}: ${err.message}`));
  page.on("console", (msg) => msg.type() === "error" && failures.push(`${query}: console error ${msg.text()}`));
  await page.goto(base + query);
  await page.waitForFunction(() => window.harnessReady);
  await page.waitForTimeout(300);
  return page;
}

// Overview: one tile per device, problems counted.
let page = await open("?view=panel");
const panel = () => page.locator("aerohive-network-panel");
check((await panel().locator(".tile").count()) === 5, "overview shows 5 device tiles");
check((await panel().locator(".summary .bad .big").textContent()).trim() === "3", "overview counts 3 problems");
await panel().locator(".tile").first().click();
await page.waitForTimeout(200);
check((await panel().locator(".faceplate .port").count()) === 52, "switch faceplate has 52 ports");

// Port dialog: PoE off, rename, uplink needs confirmation, reboot asks about unsaved changes.
await panel().locator('.faceplate [data-port="4"]').click();
await panel().locator('[data-toggle="poe"]').evaluate((el) => el.click());
await page.waitForTimeout(1000);
await panel().locator("#port-name").fill("cam-drive");
await panel().locator('[data-action="rename"]').click();
await page.waitForTimeout(1000);
check((await panel().locator('.faceplate [data-port="4"]').getAttribute("title")).includes("cam-drive"), "rename shows on the faceplate");
await panel().locator('[data-action="close"]').first().click();
await panel().locator('.faceplate [data-port="49"]').click();
await panel().locator('[data-toggle="port"]').evaluate((el) => el.click());
check((await panel().locator('[role="alertdialog"]').count()) === 1, "turning off an uplink asks first");
await panel().locator('[data-action="close"]').first().click(); // cancel -> back to the port
check((await panel().locator('[data-toggle="port"]').isChecked()) === true, "cancelled uplink stays on");
await panel().locator('[data-action="close"]').first().click();
await panel().locator('.detail-head [data-action="reboot"]').click();
check((await panel().locator('input[data-choice="save"]').count()) === 2, "reboot offers save / discard with unsaved changes");
await panel().locator('input[data-choice="save"][value="discard"]').check();
await panel().locator('[data-action="reboot-confirm"]').click();
await page.waitForTimeout(800);
const calls = await page.evaluate(() => window.serviceCalls.map((c) => [c.service, c.data]));
check(JSON.stringify(calls.map((c) => c[0])) === JSON.stringify(["set_poe", "set_port_description", "reboot"]), `actions sent: ${JSON.stringify(calls)}`);
check(calls[2] && calls[2][1].save_changes === false && calls[2][1].confirm === true, "reboot without saving sends save_changes: false");
await page.close();

// Restore flow on the SR switch.
page = await open("?view=panel&path=SR2208P");
await panel().locator('.banner [data-action="restore"]').click();
await panel().locator('[data-action="restore-confirm"]').click();
await page.waitForTimeout(1000);
check((await panel().locator(".banner.err").count()) === 0, "restoring clears the changed-after-reboot banner");
await page.close();

// Wi-Fi tab: timer, edit, add, toggle; non-admins can only look.
page = await open("?view=panel&path=wifi");
check((await panel().locator(".net").count()) === 4, "Wi-Fi tab lists 4 networks");
check((await panel().locator('.net[data-ssid="Unknown"] [data-action="wifi-edit"], .net[data-ssid="Unknown"] [data-action="wifi-timer"]').count()) === 0, "an unconfigured entry has no edit/timer");
check(await panel().locator('.net[data-ssid="Unknown"] [data-wifi-toggle]').isDisabled(), "an unconfigured entry can't be switched");
check((await panel().locator(".muted").first().textContent()).includes("2 access points"), "router with Wi-Fi counts as an access point");
await panel().locator('.net[data-ssid="Guest Wifi"] [data-action="wifi-timer"]').click();
await panel().locator('input[data-field="minutes"][value="120"]').check();
await panel().locator('[data-action="wifi-timer-start"]').click();
await page.waitForTimeout(900);
check((await panel().locator('.net[data-ssid="Guest Wifi"] .timer-note').count()) === 1, "timer shows on the network");
check(!(await panel().locator('.net[data-ssid="Guest Wifi"] [data-wifi-toggle]').isChecked()), "network is off during the timer");
await panel().locator('.net[data-ssid="AP460C"] [data-action="wifi-edit"]').click();
await panel().locator('input[data-field="bands"][value="5 GHz"]').uncheck();
await panel().locator("#wifi-password").fill("short");
await panel().locator('[data-action="wifi-save"]').click();
check((await panel().locator(".toast.err").count()) === 1, "short Wi-Fi password is refused before sending");
await panel().locator("#wifi-password").fill("long-enough-key");
await panel().locator('[data-action="wifi-save"]').click();
await page.waitForTimeout(900);
await panel().locator('[data-action="wifi-add"]').first().click();
await panel().locator("#wifi-name").fill("Kids");
await panel().locator('[data-action="wifi-create"]').click();
await page.waitForTimeout(900);
check((await panel().locator(".net").count()) === 5, "new network appears");
await panel().locator('.net[data-ssid="IoT"] [data-wifi-toggle]').evaluate((el) => el.click());
await page.waitForTimeout(900);
check(((await panel().locator(".toast").textContent()) || "").includes("IoT turned off"), "Wi-Fi toggle confirms with a message");
const wifiCalls = await page.evaluate(() => window.serviceCalls.map((c) => c.data));
check(JSON.stringify(wifiCalls) === JSON.stringify([
  { type: "aerohive_local/wifi/update", ssid: "Guest Wifi", enabled: false, duration_minutes: 120 },
  { type: "aerohive_local/wifi/update", ssid: "AP460C", password: "long-enough-key", bands: ["2.4 GHz"] },
  { type: "aerohive_local/wifi/create", ssid: "Kids", bands: ["2.4 GHz", "5 GHz"], hidden: false },
  { type: "aerohive_local/wifi/update", ssid: "IoT", enabled: false },
]), `Wi-Fi commands sent: ${JSON.stringify(wifiCalls)}`);
await page.close();
page = await open("?view=panel&path=wifi&admin=0");
check((await panel().locator('[data-action="wifi-edit"], [data-action="wifi-add"]').count()) === 0, "non-admins get no Wi-Fi controls");
check(await panel().locator(".net [data-wifi-toggle]").first().isDisabled(), "non-admin toggles are disabled");
await page.close();

// Setup tab: template, add a network and Wi-Fi, access matrix, save, add a timed key, review and apply.
page = await open("?view=panel&path=setup");
await panel().locator('[data-action="plan-template"]').click();
await page.waitForTimeout(500);
check((await panel().locator('[data-action="plan-net-edit"]').count()) === 3, "template adds its networks");
await panel().locator('[data-action="plan-net-add"]').click();
await panel().locator("#net-name").fill("Guest Wi-Fi");
await panel().locator("#net-vlan").fill("30");
await panel().locator("#net-subnet").fill("10.99.99.1/24");
await panel().locator('[data-action="plan-net-done"]').click();
check((await panel().locator(".toast.err").textContent()).includes("VLAN 30 is already used"), "duplicate VLAN is refused");
await panel().locator("#net-vlan").fill("40");
await panel().locator('[data-action="plan-net-done"]').click();
await panel().locator('input[data-plan="reach"][data-from="iso"][data-to="iot"]').check();
await panel().locator('[data-action="plan-wifi-add"]').click();
await panel().locator("#pw-ssid").fill("Guest");
await panel().locator("#pw-network").selectOption("guest_wi_fi");
await panel().locator("#plan-password").fill("guest-pass-1");
await panel().locator('[data-action="plan-wifi-done"]').click();
await panel().locator('input[data-plan="switch_trunk"]').first().fill("47-48");
await panel().locator('[data-action="plan-save"]').click(); // the field's change (on blur) must not swallow this click
await page.waitForTimeout(700);
await panel().locator('[data-action="plan-key-add"]').click();
await panel().locator("#key-name").fill("Living room TV");
await panel().locator('input[data-field="minutes"][value="15"]').check();
await panel().locator('[data-action="plan-key-create"]').click();
await page.waitForTimeout(1000);
check(((await panel().locator(".key-box").textContent()) || "").trim() === "Kx7mPq2vR9tw", "the new key is shown");
await panel().locator('.dialog [data-action="close"]').last().click();
await panel().locator('[data-action="plan-review"]').click();
await page.waitForTimeout(900);
check((await panel().locator(".apply-row").count()) === 4, "review lists the switch and access points");
await panel().locator('[data-action="plan-apply"]').click();
await page.waitForTimeout(900);
check((await panel().locator(".chip.ok").count()) === 4, "every device reports done");
await panel().locator('.dialog [data-action="close"]').last().click();
// Key dialog has a QR code; Wi-Fi with a password has a QR button.
await panel().locator('[data-action="plan-key-show"]').first().click();
await page.waitForTimeout(500);
check((await panel().locator(".qr-box svg").count()) === 1, "key dialog shows a QR code");
await panel().locator('.dialog [data-action="close"]').last().click();
await panel().locator('[data-action="plan-wifi-qr"][data-wifi="guest"]').click();
await page.waitForTimeout(500);
check(((await panel().locator(".key-box").textContent()) || "").trim() === "guest-pass-1" && (await panel().locator(".qr-box svg").count()) === 1, "Wi-Fi QR shows its password and code");
await panel().locator('.dialog [data-action="close"]').last().click();
// Onboarding: turn the portal on, approve the waiting device.
await panel().locator('input[data-plan="ob_enabled"]').evaluate((el) => el.click());
await panel().locator("#ob-wifi").selectOption("guest");
await panel().locator('[data-action="plan-save"]').click();
await page.waitForTimeout(700);
await panel().locator('[data-action="plan-ob-approve"]').click();
await panel().locator("#ap-net").selectOption("iot");
await panel().locator('[data-action="plan-ob-approve-confirm"]').click();
await page.waitForTimeout(800);
check(((await panel().locator(".toast").textContent()) || "").includes("Living room TV approved"), "approving confirms with a message");
check((await panel().locator('[data-action="plan-ob-approve"]').count()) === 0, "no device left waiting");
const planCalls = await page.evaluate(() => window.serviceCalls.map((c) => c.data));
const approve = planCalls.find((c) => c.type === "aerohive_local/plan/onboard_approve");
check(approve && approve.request_id === "021122334455" && approve.wifi === "homeiso" && approve.network === "iot" && approve.bind_mac === false, `approve request: ${JSON.stringify(approve)}`);
const obSave = planCalls.filter((c) => c.type === "aerohive_local/plan/save").pop();
check(obSave && obSave.plan.onboarding.enabled === true && obSave.plan.onboarding.wifi === "guest", "onboarding settings are saved");
const saved = planCalls.filter((c) => c.type === "aerohive_local/plan/save")[0];
check(saved && saved.secrets["wifi:guest"] === "guest-pass-1", "the Wi-Fi password is sent with the save");
check(saved && saved.plan.networks.find((n) => n.id === "iso").reach.includes("iot"), "the access matrix is saved");
check(saved && saved.plan.devices.switch_trunks && Object.values(saved.plan.devices.switch_trunks).includes("47-48"), "switch trunk ports are saved");
const keyCall = planCalls.find((c) => c.type === "aerohive_local/plan/key_add");
check(keyCall && keyCall.minutes === 15 && keyCall.wifi === "homeiso", `key request: ${JSON.stringify(keyCall)}`);
await page.close();
page = await open("?view=panel&path=setup&admin=0");
check(((await panel().locator(".content").textContent()) || "").includes("Only administrators"), "non-admins can't see the setup");
await page.close();

// Clients search, cards, phone width.
page = await open("?view=panel&path=clients");
await panel().locator("#client-search").fill("pixel");
await page.waitForTimeout(100);
check((await panel().locator("#client-results tr").count()) === 2, "client search finds 2 Pixels");
await page.close();
page = await open("?view=cards");
check((await page.locator("aerohive-switch-card").first().locator(".port").count()) === 52, "switch card renders the faceplate");
await page.close();
for (const query of ["?view=panel", "?view=panel&path=X450e-48p", "?view=panel&path=wifi", "?view=panel&path=setup", "?view=cards"]) {
  page = await open(query, { width: 360, height: 780 });
  check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), `${query}: no sideways scroll at 360 px`);
  await page.close();
}

await browser.close();
server.close();
if (failures.length) {
  console.error(`FAILED (${failures.length}):\n- ${failures.join("\n- ")}`);
  process.exit(1);
}
console.log("frontend smoke test passed");
