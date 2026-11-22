/*
 * Aerohive / Extreme Networks (Local SSH): Network panel and dashboard cards.
 *
 * One module, no build step, no external libraries. It defines:
 *   <aerohive-network-panel>          the "Network" sidebar panel
 *   custom:aerohive-switch-card        a switch faceplate with port controls
 *   custom:aerohive-device-card        status of one device (AP, switch, router)
 *   custom:aerohive-network-card       all devices at a glance
 *
 * Data comes from the integration's WebSocket subscription
 * (aerohive_local/subscribe). Changes go through the integration's actions,
 * so they get the same validation and safety checks as everywhere else.
 */

const DOMAIN = "aerohive_local";
const PANEL_PATH = "aerohive-network";

/* ------------------------------------------------------------------ */
/* Data: one shared WebSocket subscription per Home Assistant connection */
/* ------------------------------------------------------------------ */

class NetworkStore {
  constructor(connection) {
    this.connection = connection;
    this.data = null;
    this.error = null;
    this.listeners = new Set();
    this._unsub = null;
    this._starting = false;
    this._stopTimer = null;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    clearTimeout(this._stopTimer);
    if (this.data || this.error) listener(this.data);
    if (!this._unsub && !this._starting) this._start();
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) {
        this._stopTimer = setTimeout(() => this._stop(), 10000);
      }
    };
  }

  async _start() {
    this._starting = true;
    try {
      this._unsub = await this.connection.subscribeMessage(
        (snapshot) => {
          this.data = snapshot;
          this.error = null;
          this._emit();
        },
        { type: `${DOMAIN}/subscribe` },
      );
    } catch (err) {
      this.error = (err && (err.message || err.code)) || String(err);
      this._emit();
    } finally {
      this._starting = false;
    }
  }

  _stop() {
    if (this.listeners.size || !this._unsub) return;
    try {
      this._unsub();
    } catch (_err) {
      /* connection already gone */
    }
    this._unsub = null;
    this.data = null;
  }

  _emit() {
    for (const listener of this.listeners) listener(this.data);
  }
}

const stores = new WeakMap();
function getStore(hass) {
  const connection = hass.connection;
  let store = stores.get(connection);
  if (!store) {
    store = new NetworkStore(connection);
    stores.set(connection, store);
  }
  return store;
}

/* ------------------------------------------------------------------ */
/* Formatting helpers                                                   */
/* ------------------------------------------------------------------ */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtRate(kbps) {
  if (kbps == null) return "–";
  if (kbps >= 1e6) return `${(kbps / 1e6).toFixed(2)} Gbit/s`;
  if (kbps >= 1000) return `${(kbps / 1000).toFixed(1)} Mbit/s`;
  return `${Math.round(kbps)} kbit/s`;
}

function fmtBytes(bytes) {
  if (bytes == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function fmtUptime(seconds) {
  if (seconds == null) return "–";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtSpeed(mbps) {
  if (!mbps) return "";
  return mbps >= 1000 ? `${mbps / 1000} Gbit/s` : `${mbps} Mbit/s`;
}

function fmtAgo(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function fmtPct(value) {
  return value == null ? "–" : `${Math.round(value)}%`;
}

const natKey = (name) => (String(name).match(/\d+/g) || []).map(Number);
function natCompare(a, b) {
  const ka = natKey(a);
  const kb = natKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i += 1) {
    const diff = (ka[i] ?? -1) - (kb[i] ?? -1);
    if (diff) return diff;
  }
  return String(a).localeCompare(String(b));
}
const portNumber = (name) => {
  const match = String(name).match(/(\d+)$/);
  return match ? match[1] : String(name);
};

function findDevice(data, ref) {
  if (!data || !data.devices) return null;
  if (!ref) return null;
  const key = String(ref).toLowerCase();
  return (
    data.devices.find((d) =>
      [d.entry_id, d.device_id, d.name, d.host].some((v) => v && String(v).toLowerCase() === key),
    ) || null
  );
}

/** MAC -> friendly name, from the wireless client lists of every AP. */
function clientNames(data) {
  const names = new Map();
  for (const device of data?.devices || []) {
    for (const sta of device.stations || []) {
      if (sta.hostname) names.set(sta.mac, sta.hostname);
    }
  }
  for (const [mac, info] of Object.entries(data?.clients || {})) {
    if (info.name) names.set(mac, info.name);
  }
  return names;
}

const KIND_LABELS = {
  phone: "Phone", tablet: "Tablet", computer: "Computer", tv: "TV / streaming", speaker: "Speaker", console: "Game console",
  printer: "Printer", camera: "Camera", "smart home": "Smart home", network: "Network device",
};

/* Everything the panel knows about one client: directory info plus where it's connected. */
function clientInfo(data, mac) {
  const info = { ...((data && data.clients && data.clients[mac]) || {}) };
  info.mac = mac;
  const devices = data?.devices || [];
  for (const device of devices) {
    const sta = (device.stations || []).find((x) => x.mac === mac);
    if (sta) {
      info.wifi = { device, sta, radio: (device.radios || []).find((r) => r.name === sta.radio) };
      info.ip = info.ip || sta.ip;
      info.hostname = info.hostname || sta.hostname;
    }
    for (const port of device.ports || []) {
      const hit = (port.clients || []).find((c) => c.mac === mac);
      // The edge port: the one with the fewest devices learned on it.
      if (hit && !port.uplink && !info.wifi && (!info.wired || (port.clients || []).length < (info.wired.port.clients || []).length)) info.wired = { device, port, vlan: hit.vlan };
    }
  }
  if (!info.wifi && !info.wired) {
    const router = devices.find((d) => (d.lan_clients || []).includes(mac));
    if (router) info.wired = { device: router, port: null, vlan: null };
  }
  if (info.wifi) delete info.wired; // seen on Wi-Fi: the switch port is just the way to its access point
  return info;
}

function clientTitle(info) {
  return info.name || (info.vendor ? `${info.vendor} device` : info.private ? "Device with a private address" : "Unknown device");
}

/** Wi-Fi networks across access points: name -> APs, bands, clients, timer. */
function wifiNetworks(data) {
  const aps = (data?.devices || []).filter((d) => d.online && ((d.radios || []).length || (d.ssids || []).length));
  const map = new Map();
  for (const device of aps) {
    for (const ssid of device.ssids || []) {
      let net = map.get(ssid.name);
      if (!net) {
        net = { name: ssid.name, aps: [], clients: 0, hidden: false, on: false, bands: new Set(), security: null, maxClients: null, configurable: false };
        map.set(ssid.name, net);
      }
      const bands = (ssid.radios || []).map((r) => (device.radios || []).find((x) => x.name === r)?.band).filter(Boolean);
      net.aps.push({ device, ssid, bands });
      // An AP can list an entry its configuration doesn't define (e.g. "Unknown"): shown, never changed.
      net.configurable = net.configurable || ssid.configurable !== false;
      net.clients += ssid.clients || 0;
      net.hidden = net.hidden || Boolean(ssid.hidden);
      net.on = net.on || (ssid.radios || []).length > 0;
      bands.forEach((b) => net.bands.add(b));
      net.security = net.security || ssid.security;
      if (net.maxClients == null) net.maxClients = ssid.max_clients;
    }
  }
  const timers = new Map((data?.wifi_timers || []).map((t) => [t.ssid, t]));
  const allBands = [...new Set(aps.flatMap((d) => (d.radios || []).map((r) => r.band).filter(Boolean)))].sort();
  const networks = [...map.values()]
    .map((n) => ({ ...n, bands: [...n.bands].sort(), timer: timers.get(n.name) || null }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return { aps, networks, allBands: allBands.length ? allBands : ["2.4 GHz", "5 GHz"] };
}

function fmtUntil(iso) {
  if (!iso) return "";
  const when = new Date(iso);
  const sameDay = when.toDateString() === new Date().toDateString();
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${when.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function wifiStatus(net) {
  if (net.timer) return `${net.timer.enabled ? "On" : "Off"} until ${fmtUntil(net.timer.until)}`;
  if (!net.on) return "Off";
  return net.bands.length ? `On · ${net.bands.join(" + ")}` : "On";
}

const WIFI_DURATIONS = [
  [30, "30 minutes"],
  [60, "1 hour"],
  [120, "2 hours"],
  [240, "4 hours"],
  [480, "8 hours"],
  [1440, "24 hours"],
];

/* ------------------------------------------------------------------ */
/* Icons (inline, 24x24 line icons)                                     */
/* ------------------------------------------------------------------ */

const ICON_PATHS = {
  switch: '<rect x="2.5" y="7" width="19" height="10" rx="2"/><path d="M6 11h1M9 11h1M12 11h1M15 11h1M6 14h1M9 14h1M12 14h1M15 14h1M18.5 12.5h.01"/>',
  ap: '<circle cx="12" cy="13" r="1.6"/><path d="M8.5 9.5a5 5 0 0 1 7 0M6 7a8.5 8.5 0 0 1 12 0M12 15v5M8 20h8"/>',
  router: '<rect x="3" y="12" width="18" height="7" rx="2"/><path d="M7 15.5h.01M10.5 15.5h.01M7 12 5.5 5M17 12l1.5-7"/>',
  unknown: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01"/>',
  bolt: '<path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H13z"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  warn: '<path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4.5M12 17h.01"/>',
  save: '<path d="M5 3h11l3 3v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v5h8V3M7 21v-7h10v7"/>',
  backup: '<path d="M3 5h18v4H3zM5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>',
  reboot: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v5h5M12 8v4l3 2"/>',
  cycle: '<path d="M21 12a9 9 0 0 1-15.5 6.2M3 12a9 9 0 0 1 15.5-6.2M18.5 2v4h-4M5.5 22v-4h4"/>',
  clients: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18.5 20a6.5 6.5 0 0 0-3-5.5"/>',
  wifi: '<path d="M2 8.5a15 15 0 0 1 20 0M5 12a10.5 10.5 0 0 1 14 0M8.5 15.5a5.5 5.5 0 0 1 7 0M12 19h.01"/>',
  timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9.5 2h5"/>',
  console: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  usb: '<path d="M12 21V5M9 7.5 12 4l3 3.5M7 10v3l5 3M17 9v3l-5 3"/><circle cx="12" cy="19.5" r="1.5"/>',
  power: '<path d="M12 3v8M7 6.3a7.5 7.5 0 1 0 10 0"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
};

const icon = (name, cls = "") =>
  `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON_PATHS[name] || ICON_PATHS.unknown}</svg>`;

/* ------------------------------------------------------------------ */
/* Styles                                                               */
/* ------------------------------------------------------------------ */

const BASE_CSS = `
:host {
  --ah-text: var(--primary-text-color, #1c1f23);
  --ah-muted: var(--secondary-text-color, #5d6570);
  --ah-card: var(--card-background-color, var(--ha-card-background, #ffffff));
  --ah-bg: var(--primary-background-color, #f3f4f6);
  --ah-line: var(--divider-color, rgba(0, 0, 0, 0.12));
  --ah-accent: var(--primary-color, #0b78d0);
  --ah-ok: var(--success-color, #2f9e55);
  --ah-warn: var(--warning-color, #d68a00);
  --ah-err: var(--error-color, #d93a2f);
  --ah-poe: #e8a600;
  --ah-radius: var(--ha-card-border-radius, 12px);
  --ah-port-fast: var(--ah-ok);
  --ah-port-slow: #c9a227;
  --ah-port-down: color-mix(in srgb, var(--ah-text) 10%, var(--ah-card));
  --ah-face: color-mix(in srgb, var(--ah-text) 7%, var(--ah-card));
  color: var(--ah-text);
  font-family: var(--paper-font-body1_-_font-family, var(--ha-font-family-body, Roboto, "Segoe UI", system-ui, sans-serif));
  font-size: 14px;
  line-height: 1.4;
}
* { box-sizing: border-box; }
.icon { width: 18px; height: 18px; flex: none; }
.spin { animation: ah-spin 0.9s linear infinite; }
@keyframes ah-spin { to { transform: rotate(360deg); } }
button { font: inherit; color: inherit; }
.muted { color: var(--ah-muted); }
.num { font-variant-numeric: tabular-nums; }
.row { display: flex; align-items: center; gap: 8px; }
.grow { flex: 1; min-width: 0; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.btn {
  display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px;
  border-radius: 18px; border: 1px solid var(--ah-line); background: var(--ah-card);
  cursor: pointer; font-weight: 500; white-space: nowrap;
}
.btn:hover:not(:disabled) { background: color-mix(in srgb, var(--ah-accent) 8%, var(--ah-card)); }
.btn:focus-visible, .port:focus-visible, .tile:focus-visible, .tab:focus-visible, .icon-btn:focus-visible {
  outline: 2px solid var(--ah-accent); outline-offset: 2px;
}
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.primary { background: var(--ah-accent); border-color: var(--ah-accent); color: var(--text-primary-color, #fff); }
.btn.primary:hover:not(:disabled) { background: color-mix(in srgb, #000 12%, var(--ah-accent)); }
.btn.danger { color: var(--ah-err); }
.btn.attention { border-color: var(--ah-warn); color: var(--ah-warn); }
.icon-btn {
  display: inline-grid; place-items: center; width: 36px; height: 36px; border-radius: 50%;
  border: 0; background: transparent; cursor: pointer; color: var(--ah-muted);
}
.icon-btn:hover { background: color-mix(in srgb, var(--ah-text) 8%, transparent); color: var(--ah-text); }

.card {
  background: var(--ah-card); border-radius: var(--ah-radius);
  border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--ah-line));
  box-shadow: var(--ha-card-box-shadow, none);
}
.section-title { font-size: 13px; font-weight: 600; color: var(--ah-muted); text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 10px; }

.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ah-muted); flex: none; }
.dot.ok { background: var(--ah-ok); }
.dot.err { background: var(--ah-err); }
.dot.warn { background: var(--ah-warn); }

.chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 10px; font-size: 12px;
  background: color-mix(in srgb, var(--ah-text) 8%, transparent); color: var(--ah-muted); white-space: nowrap;
}
.chip.err { background: color-mix(in srgb, var(--ah-err) 14%, transparent); color: var(--ah-err); }
.chip.warn { background: color-mix(in srgb, var(--ah-warn) 16%, transparent); color: color-mix(in srgb, var(--ah-warn) 75%, var(--ah-text)); }
.chip.ok { background: color-mix(in srgb, var(--ah-ok) 14%, transparent); color: color-mix(in srgb, var(--ah-ok) 80%, var(--ah-text)); }
.chip .icon { width: 13px; height: 13px; }

/* Faceplate */
.faceplate {
  display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px 18px; padding: 12px 14px;
  background: var(--ah-face); border-radius: 10px; border: 1px solid var(--ah-line);
}
.fp-slot { font-size: 11px; color: var(--ah-muted); writing-mode: vertical-rl; transform: rotate(180deg); align-self: center; }
.fp-block { display: flex; gap: 4px; }
.fp-block.extra { padding-left: 14px; border-left: 1px dashed var(--ah-line); }
.fp-col { display: flex; flex-direction: column; align-items: center; gap: 3px; }
.fp-num { font-size: 10px; line-height: 12px; height: 12px; color: var(--ah-muted); font-variant-numeric: tabular-nums; }
.port {
  position: relative; width: 30px; height: 24px; border-radius: 4px; padding: 0; cursor: pointer;
  border: 1px solid color-mix(in srgb, var(--ah-text) 18%, transparent);
  background: var(--ah-port-down); transition: transform 0.08s, box-shadow 0.08s;
}
.port::after {
  content: ""; position: absolute; left: 50%; width: 10px; height: 4px; transform: translateX(-50%);
  background: color-mix(in srgb, #000 28%, transparent); border-radius: 1px;
}
.fp-col .port:first-of-type::after { bottom: 2px; }
.fp-col .port:last-of-type::after { top: 2px; }
.port:hover { transform: translateY(-1px); box-shadow: 0 2px 6px rgba(0, 0, 0, 0.18); }
.port.s-fast { background: var(--ah-port-fast); border-color: transparent; }
.port.s-up { background: color-mix(in srgb, var(--ah-port-fast) 55%, var(--ah-port-slow)); border-color: transparent; }
.port.s-slow { background: var(--ah-port-slow); border-color: transparent; }
.port.s-off {
  background: repeating-linear-gradient(135deg, var(--ah-port-down) 0 4px, color-mix(in srgb, var(--ah-text) 18%, var(--ah-card)) 4px 6px);
}
.port.s-np { background: transparent; border-style: dashed; }
/* SFP cages: square, no RJ45 latch; an empty cage shows its slot. */
.port.m-sfp, .port.m-sfp-empty { width: 24px; border-width: 2px; border-color: color-mix(in srgb, var(--ah-text) 40%, transparent); border-radius: 2px; }
.port.m-sfp::after, .port.m-sfp-empty::after { display: none; }
.port.m-sfp-empty { background: var(--ah-face); box-shadow: inset 0 0 0 3px var(--ah-face), inset 0 0 0 7px color-mix(in srgb, #000 30%, transparent); }
/* Combo (copper or SFP): small square marker. */
.port.m-combo::before {
  content: ""; position: absolute; right: 2px; bottom: 2px; width: 6px; height: 6px; border-radius: 1px;
  border: 1.5px solid color-mix(in srgb, var(--ah-text) 55%, transparent); background: var(--ah-face);
}
.fp-col .port.m-combo:last-of-type::before { bottom: auto; top: 2px; }
.fp-block.hw { align-self: center; }
.hw-tile {
  position: relative; display: grid; place-items: center; width: 30px; height: 24px; border-radius: 4px;
  border: 1px solid color-mix(in srgb, var(--ah-text) 22%, transparent); background: var(--ah-port-down); color: var(--ah-muted);
}
.hw-tile .icon { width: 14px; height: 14px; }
.hw-tile.ok { background: var(--ah-port-fast); color: #fff; border-color: transparent; }
.hw-tile.err { background: var(--ah-err); color: #fff; border-color: transparent; }
.hw-tile.vacant { background: transparent; border-style: dashed; }
.hw-tile.info { background: color-mix(in srgb, var(--ah-accent) 16%, var(--ah-card)); color: var(--ah-accent); }
.port.s-np::after { display: none; }
.port.sel { outline: 2px solid var(--ah-accent); outline-offset: 2px; }
.port.dim { opacity: 0.3; }
.port .badge {
  position: absolute; display: grid; place-items: center; width: 13px; height: 13px; border-radius: 50%;
  border: 1.5px solid var(--ah-face); color: #fff;
}
.port .badge .icon { width: 9px; height: 9px; stroke-width: 3; }
.port .b-poe { right: -6px; top: -6px; background: var(--ah-poe); }
.port .b-up { left: -6px; top: -6px; background: var(--ah-accent); }
.port .b-err { right: -6px; bottom: -6px; background: var(--ah-err); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; font-size: 12px; color: var(--ah-muted); }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.legend i { display: inline-block; width: 14px; height: 11px; border-radius: 2px; border: 1px solid var(--ah-line); }
.legend i.sfp { width: 11px; border: 2px solid color-mix(in srgb, var(--ah-text) 40%, transparent); background: var(--ah-port-fast); }
.legend i.combo { position: relative; background: var(--ah-port-down); }
.legend i.combo::before { content: ""; position: absolute; left: 1px; bottom: 1px; width: 4px; height: 4px; border: 1px solid var(--ah-muted); }

.mini-fp { display: flex; flex-wrap: wrap; gap: 2px 6px; }
.mini-fp .blk { display: grid; grid-auto-flow: column; grid-template-rows: repeat(2, 7px); gap: 2px; }
.mini-fp b { width: 7px; height: 7px; border-radius: 1.5px; background: var(--ah-port-down); }
.mini-fp b.s-fast { background: var(--ah-port-fast); }
.mini-fp b.s-up { background: color-mix(in srgb, var(--ah-port-fast) 55%, var(--ah-port-slow)); }
.mini-fp b.s-slow { background: var(--ah-port-slow); }
.mini-fp b.s-off { background: color-mix(in srgb, var(--ah-text) 30%, var(--ah-card)); }
.mini-fp b.s-np { background: transparent; outline: 1px dashed var(--ah-line); outline-offset: -1px; }

/* Meters */
.meter { height: 6px; border-radius: 3px; background: color-mix(in srgb, var(--ah-text) 10%, transparent); overflow: hidden; }
.meter > i { display: block; height: 100%; background: var(--ah-accent); border-radius: 3px; }
.meter > i.warn { background: var(--ah-warn); }
.meter > i.err { background: var(--ah-err); }

/* Key/value lists */
.kv { display: grid; grid-template-columns: minmax(96px, auto) 1fr; gap: 6px 14px; margin: 0; }
.kv dt { color: var(--ah-muted); }
.kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }

/* Toggle */
.toggle { display: inline-flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.toggle input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.toggle .track {
  position: relative; width: 36px; height: 20px; border-radius: 10px; flex: none;
  background: color-mix(in srgb, var(--ah-text) 25%, transparent); transition: background 0.15s;
}
.toggle .track::after {
  content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%;
  background: #fff; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3); transition: transform 0.15s;
}
.toggle input:checked + .track { background: var(--ah-accent); }
.toggle input:checked + .track::after { transform: translateX(16px); }
.toggle input:focus-visible + .track { outline: 2px solid var(--ah-accent); outline-offset: 2px; }
.toggle input:disabled + .track { opacity: 0.45; }

/* Dialog */
.scrim {
  position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.42);
  display: flex; align-items: center; justify-content: center; padding: 16px;
}
.dialog {
  width: min(460px, 100%); max-height: calc(100vh - 32px); overflow: auto; background: var(--ah-card);
  border-radius: 16px; box-shadow: 0 12px 40px rgba(0, 0, 0, 0.3); padding: 20px;
}
.dialog h2 { margin: 0; font-size: 20px; font-weight: 500; }
.dialog .dlg-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 14px; }
.dialog .dlg-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 20px; }
.dialog section { padding: 14px 0; border-top: 1px solid var(--ah-line); }
.dialog section:first-of-type { border-top: 0; padding-top: 4px; }
.controls { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: center; }
.field { display: flex; gap: 8px; align-items: center; }
.field input[type="text"] {
  flex: 1; min-width: 0; height: 36px; padding: 0 10px; border-radius: 8px; font: inherit; color: var(--ah-text);
  border: 1px solid var(--ah-line); background: var(--ah-bg);
}
.field input[type="text"]:focus { outline: 2px solid var(--ah-accent); outline-offset: -1px; }
.choice { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--ah-line); border-radius: 10px; cursor: pointer; margin-top: 8px; }
.choice input { margin-top: 3px; accent-color: var(--ah-accent); }
.client-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.client-list li { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.mono { font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12.5px; }

.checks { display: flex; flex-wrap: wrap; gap: 8px 18px; }
.checks label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.checks input { accent-color: var(--ah-accent); width: 16px; height: 16px; }
.form-row { margin-top: 14px; }
.form-row > .section-title { display: block; margin-bottom: 6px; }
.field input[type="password"], .field input[type="number"] {
  flex: 1; min-width: 0; height: 36px; padding: 0 10px; border-radius: 8px; font: inherit; color: var(--ah-text);
  border: 1px solid var(--ah-line); background: var(--ah-bg);
}
.field input:focus { outline: 2px solid var(--ah-accent); outline-offset: -1px; }
.kv a { color: var(--ah-accent); text-decoration: none; }
.kv a:hover { text-decoration: underline; }
.link-btn { border: 0; background: transparent; padding: 0; font: inherit; color: inherit; text-align: left; cursor: pointer; }
.link-btn:hover { color: var(--ah-accent); }
.port-select { height: 36px; padding: 0 8px; border-radius: 8px; border: 1px solid var(--ah-line); background: var(--ah-bg); color: var(--ah-text); font: inherit; min-width: 0; }
.speed-row { flex-wrap: wrap; }
.speed-row .port-select { flex: 1 1 140px; }
.hint { font-size: 12px; color: var(--ah-muted); margin-top: 6px; }
.hint.err { color: var(--ah-err); }
.durations { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
.durations .choice { margin-top: 0; align-items: center; }
.net { display: flex; flex-direction: column; gap: 10px; padding: 16px; }
.net-head { display: flex; align-items: center; gap: 12px; }
.net-name { font-size: 16px; font-weight: 500; overflow-wrap: anywhere; }
.net-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.net-actions .grow { min-width: 8px; }
.timer-note { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 10px;
  background: color-mix(in srgb, var(--ah-accent) 10%, transparent); font-size: 13px; }
.toast {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 20; max-width: calc(100% - 32px);
  background: #323232; color: #fff; padding: 12px 18px; border-radius: 8px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
}
.toast.err { background: var(--ah-err); }
.empty { padding: 32px 16px; text-align: center; color: var(--ah-muted); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
`;

/* ------------------------------------------------------------------ */
/* Shared rendering                                                     */
/* ------------------------------------------------------------------ */

function portState(port) {
  if (port.present === false) return "s-np";
  if (port.enabled === false) return "s-off";
  if (!port.up) return "s-down";
  if (!port.speed || port.speed >= 1000) return "s-fast";
  if (port.speed >= 100) return "s-up";
  return "s-slow";
}

function portStatusText(port) {
  if (port.present === false) return "Not present";
  if (port.enabled === false) return "Disabled";
  if (!port.up) return "No link";
  return ["Up", fmtSpeed(port.speed), port.duplex].filter(Boolean).join(" · ");
}

function mediaText(port) {
  switch (port.media) {
    case "sfp":
      return `SFP${port.module ? ` (${port.module})` : ""}`;
    case "sfp-empty":
      return "SFP, no module";
    case "combo":
      return `combo: copper or SFP${port.module ? ` (${port.module} inserted)` : ""}`;
    case "copper":
      return "copper";
    default:
      return "";
  }
}

function portTitle(port) {
  const parts = [`Port ${port.name}${port.label ? ` (${port.label})` : ""}: ${portStatusText(port)}`];
  if (port.media && port.media !== "copper") parts.push(mediaText(port));
  if (port.poe && port.poe.delivering) parts.push(`PoE ${port.poe.power_w ?? 0} W`);
  if (port.neighbor && port.neighbor.name) parts.push(`→ ${port.neighbor.name}`);
  if (port.uplink) parts.push("uplink");
  return parts.join(", ");
}

/** Ports grouped like the front of a switch: stack members, blocks of 12, uplinks apart. */
function faceplateLayout(ports) {
  const bySlot = new Map();
  for (const port of [...ports].sort((a, b) => natCompare(a.name, b.name))) {
    const slot = String(port.name).includes(":") ? String(port.name).split(":")[0] : "";
    if (!bySlot.has(slot)) bySlot.set(slot, []);
    bySlot.get(slot).push(port);
  }
  const rows = [];
  for (const [slot, list] of bySlot) {
    const count = list.length;
    let main = count > 8 ? Math.floor(count / 8) * 8 : count;
    if (count - main > 8) main = count;
    const blockSize = main >= 24 ? 12 : main || 1;
    const blocks = [];
    for (let i = 0; i < main; i += blockSize) blocks.push({ ports: list.slice(i, i + blockSize), extra: false });
    if (count > main) blocks.push({ ports: list.slice(main), extra: true });
    rows.push({ slot, blocks });
  }
  return rows;
}

function columns(ports) {
  const cols = [];
  for (let i = 0; i < ports.length; i += 2) cols.push([ports[i], ports[i + 1]]);
  return cols;
}

const isRouterPorts = (ports) => ports.length > 0 && ports.every((p) => /^(eth\d+|lan\d*|wan\d*)$/i.test(p.name));
/* WAN: named wan*, else eth0 on a branch router whose ports are all ethN. */
const isWanPort = (port, ports) =>
  /^wan/i.test(port.name) || (!ports.some((p) => /^wan/i.test(p.name)) && ports.every((p) => /^eth\d+$/i.test(p.name)) && port.name.toLowerCase() === "eth0");

function renderFaceplate(device, { selected = null, filter = null } = {}) {
  const ports = device.ports || [];
  if (!ports.length) return `<div class="empty">No ports reported yet.</div>`;
  if (isRouterPorts(ports)) {
    // Branch routers (BR100): one row, WAN (eth0) then the LAN ports.
    const sorted = [...ports].sort((a, b) => natCompare(a.name, b.name));
    const wan = sorted.filter((p) => isWanPort(p, sorted));
    const lan = sorted.filter((p) => !isWanPort(p, sorted));
    const col = (p, label) => `<div class="fp-col"><span class="fp-num">${esc(label)}</span>${renderPort(p, selected, filter)}</div>`;
    return `<div class="faceplate" role="group" aria-label="Router ports">
      ${wan.length ? `<div class="fp-block">${wan.map((p, i) => col(p, wan.length > 1 ? `WAN${i + 1}` : "WAN")).join("")}</div>` : ""}
      <div class="fp-block ${wan.length ? "extra" : ""}">${lan.map((p) => col(p, portNumber(p.name))).join("")}</div>
      ${renderHardware(device)}
    </div>`;
  }
  const rows = faceplateLayout(ports);
  return rows
    .map(
      (row, rowIndex) => `
      <div class="faceplate" role="group" aria-label="${esc(row.slot ? `Switch ${row.slot} ports` : "Switch ports")}">
        ${row.slot ? `<span class="fp-slot">Slot ${esc(row.slot)}</span>` : ""}
        ${row.blocks
          .map(
            (block) => `
          <div class="fp-block ${block.extra ? "extra" : ""}">
            ${columns(block.ports)
              .map(
                ([top, bottom]) => `
              <div class="fp-col">
                <span class="fp-num">${esc(portNumber(top.name))}</span>
                ${renderPort(top, selected, filter)}
                ${bottom ? renderPort(bottom, selected, filter) : '<span style="height:24px"></span>'}
                <span class="fp-num">${bottom ? esc(portNumber(bottom.name)) : ""}</span>
              </div>`,
              )
              .join("")}
          </div>`,
          )
          .join("")}
        ${rowIndex === rows.length - 1 ? renderHardware(device) : ""}
      </div>`,
    )
    .join("");
}

/** Console, management port, USB and power supplies, where the device reports them. */
function renderHardware(device) {
  const hw = device.hardware || {};
  const tiles = [];
  const tile = (label, cls, iconName, title) =>
    tiles.push(`<div class="fp-col"><span class="fp-num">${esc(label)}</span><div class="hw-tile ${cls}" title="${esc(title)}" aria-label="${esc(title)}">${icon(iconName)}</div></div>`);
  if (hw.console) tile("CON", "info", "console", "Serial console port");
  if (hw.mgmt) {
    const link = hw.mgmt.link;
    tile("MGMT", link ? "ok" : link === false ? "" : "info", "switch",
      `Management port: ${link ? "link up" : link === false ? "no link" : "link unknown"}${hw.mgmt.ip ? `, ${hw.mgmt.ip}` : ""}`);
  }
  for (const dev of hw.usb || []) tile("USB", dev.up === false ? "" : "ok", "usb", `USB: ${dev.name}${dev.id ? ` (${dev.id})` : ""}`);
  for (const psu of hw.psus || []) {
    const state = String(psu.state || "");
    const cls = /empty|not present/i.test(state) ? "vacant" : /fail|fault|unsupported|off/i.test(state) ? "err" : /on|ok|present/i.test(state) ? "ok" : "";
    tile(psu.name, cls, "power", `${psu.name}: ${state}`);
  }
  return tiles.length ? `<div class="fp-block extra hw" role="group" aria-label="Other connectors">${tiles.join("")}</div>` : "";
}

function renderPort(port, selected, filter) {
  const classes = ["port", portState(port)];
  if (port.media && port.media !== "copper") classes.push(`m-${port.media}`);
  if (selected === port.name) classes.push("sel");
  if (filter && !filter(port)) classes.push("dim");
  const badges = [
    port.poe && port.poe.delivering ? `<span class="badge b-poe">${icon("bolt")}</span>` : "",
    port.uplink ? `<span class="badge b-up">${icon("up")}</span>` : "",
  ].join("");
  return `<button class="${classes.join(" ")}" data-port="${esc(port.name)}" title="${esc(portTitle(port))}" aria-label="${esc(portTitle(port))}">${badges}</button>`;
}

function renderLegend(device) {
  const media = new Set(((device && device.ports) || []).map((p) => p.media));
  return `<div class="legend" aria-hidden="true">
    ${media.has("sfp") || media.has("sfp-empty") ? '<span><i class="sfp"></i>SFP</span>' : ""}
    ${media.has("combo") ? '<span><i class="combo"></i>Copper or SFP</span>' : ""}
    <span><i style="background:var(--ah-port-fast)"></i>1 Gbit/s+</span>
    <span><i style="background:color-mix(in srgb, var(--ah-port-fast) 55%, var(--ah-port-slow))"></i>100 Mbit/s</span>
    <span><i style="background:var(--ah-port-slow)"></i>10 Mbit/s</span>
    <span><i style="background:var(--ah-port-down)"></i>No link</span>
    <span><i style="background:repeating-linear-gradient(135deg,var(--ah-port-down) 0 3px,color-mix(in srgb,var(--ah-text) 18%,var(--ah-card)) 3px 5px)"></i>Disabled</span>
    <span><i style="background:var(--ah-poe);border-radius:50%;width:11px"></i>PoE</span>
    <span><i style="background:var(--ah-accent);border-radius:50%;width:11px"></i>Uplink</span>
  </div>`;
}

function renderMiniFaceplate(device) {
  const ports = [...(device.ports || [])].sort((a, b) => natCompare(a.name, b.name));
  if (!ports.length) return "";
  if (isRouterPorts(ports)) {
    return `<div class="mini-fp" aria-hidden="true"><div class="blk" style="grid-template-rows:7px">${ports.map((p) => `<b class="${portState(p)}"></b>`).join("")}</div></div>`;
  }
  const blocks = [];
  for (let i = 0; i < ports.length; i += 12) blocks.push(ports.slice(i, i + 12));
  return `<div class="mini-fp" aria-hidden="true">${blocks
    .map((b) => `<div class="blk">${b.map((p) => `<b class="${portState(p)}"></b>`).join("")}</div>`)
    .join("")}</div>`;
}

function deviceIcon(device) {
  return icon({ switch: "switch", ap: "ap", router: "router" }[device.kind] || "unknown");
}

function deviceSummary(device) {
  if (!device.online) return "Offline";
  if (device.kind === "unknown") return "Connected, but the device type wasn't recognised";
  if (device.kind === "switch" || device.kind === "router") {
    const ports = device.ports || [];
    const up = ports.filter((p) => p.up).length;
    const parts = [`${up}/${ports.length} ports up`];
    if (device.poe && device.poe.used_w != null) parts.push(`${device.poe.used_w} W PoE`);
    if (device.kind === "router" && device.stations?.length) parts.push(`${device.stations.length} Wi-Fi clients`);
    return parts.join(" · ");
  }
  const clients = (device.stations || []).length;
  const ssids = (device.ssids || []).filter((s) => s.broadcasting).length;
  return `${clients} client${clients === 1 ? "" : "s"} · ${ssids} SSID${ssids === 1 ? "" : "s"} broadcasting`;
}

function problemChips(device, max = 3) {
  const problems = device.online ? device.problems || [] : [{ kind: "offline", text: "Offline" }];
  const shown = problems.slice(0, max).map((p) => {
    const cls = p.kind === "unsaved" ? "warn" : "err";
    return `<span class="chip ${cls}">${icon("warn")}${esc(p.text)}</span>`;
  });
  if (problems.length > max) shown.push(`<span class="chip">+${problems.length - max}</span>`);
  return shown.join("");
}

function meterClass(value, warn = 70, err = 90) {
  if (value == null) return "";
  if (value >= err) return "err";
  if (value >= warn) return "warn";
  return "";
}

/* ------------------------------------------------------------------ */
/* Base element: data subscription, actions, dialogs, toasts            */
/* ------------------------------------------------------------------ */

class AerohiveBase extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._unsubStore = null;
    this._dialog = null; // {type, ...}
    this._busy = new Set();
    this._optimistic = new Map(); // "entry:port:field" -> {value, until}: what the user just chose
    this._toastTimer = null;
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
    this.shadowRoot.addEventListener("input", (ev) => {
      const target = ev.target;
      if (!target.dataset || !target.dataset.field || target.type === "checkbox" || target.type === "radio") return;
      this._captureField(target);
      if (target.id === "wifi-password") {
        // The dialog isn't re-rendered while typing; update the hint in place.
        const hint = target.closest(".form-row").querySelector(".hint");
        const bad = target.value && (target.value.length < 8 || target.value.length > 63);
        hint.classList.toggle("err", Boolean(bad));
        hint.textContent = bad ? "A Wi-Fi password needs 8 to 63 characters." : "Sent straight to the access points; never stored in Home Assistant.";
      }
    });
    this.shadowRoot.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && this._dialog) this._closeDialog();
      if (ev.key === "Enter" && ev.target.matches && ev.target.matches("input[data-enter]")) {
        ev.preventDefault();
        this._handleAction(ev.target.dataset.enter, ev.target);
      }
    });
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || (this._store && this._store.connection !== hass.connection)) this._connectStore();
    this._onHass();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._hass && !this._unsubStore) this._connectStore();
    this._render();
  }

  disconnectedCallback() {
    if (this._unsubStore) this._unsubStore();
    this._unsubStore = null;
  }

  _connectStore() {
    if (this._unsubStore) this._unsubStore();
    this._store = getStore(this._hass);
    this._unsubStore = this._store.subscribe((data) => this._onData(data));
  }

  _onHass() {}

  _onData(data) {
    // A change only shows once the device has been read again: keep its control busy until then.
    if (this._held && this._held.size) {
      for (const key of this._held) this._busy.delete(key);
      this._held.clear();
    }
    this._data = this._withOptimistic(data);
    this._render();
  }

  /* Show what the user just chose until the device reports it (or 15 s pass). */
  _setOptimistic(device, port, field, value) {
    this._optimistic.set(`${device.entry_id}\u0000${port}\u0000${field}`, { value, until: Date.now() + 15000 });
    this._data = this._withOptimistic(this._store ? this._store.data : this._data);
    this._render();
  }

  _clearOptimistic(device, port, field) {
    this._optimistic.delete(`${device.entry_id}\u0000${port}\u0000${field}`);
    this._data = this._withOptimistic(this._store ? this._store.data : this._data);
    this._render();
  }

  _withOptimistic(data) {
    if (!data || !this._optimistic.size) return data;
    const now = Date.now();
    const copy = { ...data, devices: data.devices.map((d) => ({ ...d, ports: (d.ports || []).map((p) => ({ ...p, poe: p.poe ? { ...p.poe } : p.poe })) })) };
    for (const [key, entry] of [...this._optimistic]) {
      const [entryId, portName, field] = key.split("\u0000");
      const port = copy.devices.find((d) => d.entry_id === entryId)?.ports.find((p) => p.name === portName);
      const real = port ? (field === "poe" ? port.poe && port.poe.enabled : port.enabled) : undefined;
      if (!port || entry.until < now || real === entry.value) {
        this._optimistic.delete(key);
        continue;
      }
      if (field === "poe" && port.poe) {
        port.poe.enabled = entry.value;
        if (!entry.value) port.poe.delivering = false;
      } else if (field === "port") {
        port.enabled = entry.value;
        if (!entry.value) port.up = false;
      }
    }
    return copy;
  }

  /* Rendering keeps an input that has focus (e.g. renaming a port) intact. */
  _dialogHasFocus() {
    const active = this.shadowRoot.activeElement;
    return Boolean(active && active.closest && active.closest(".dialog") && active.matches("input[type=text], input[type=password], input[type=number], textarea"));
  }

  _renderDialogInto(host) {
    if (!host) return;
    const force = this._forceDialog;
    this._forceDialog = false;
    if (!force && this._dialogHasFocus()) return;
    // Keep keyboard focus on the same control across re-renders.
    const active = this.shadowRoot.activeElement;
    let keep = null;
    if (active && host.contains(active)) {
      for (const attr of ["data-toggle", "data-action", "data-choice"]) {
        if (active.hasAttribute(attr)) {
          keep = `[${attr}="${active.getAttribute(attr)}"]${active.value && attr === "data-choice" ? `[value="${active.value}"]` : ""}`;
          break;
        }
      }
    }
    host.innerHTML = this._dialog ? `<div class="scrim" data-action="scrim">${this._dialogHtml()}</div>` : "";
    const wanted = (this._dialog && this._dialog.focus) || keep;
    if (this._dialog) this._dialog.focus = null;
    if (wanted) {
      const el = host.querySelector(wanted);
      if (el) el.focus();
    }
  }

  _dialogHtml() {
    const d = this._dialog;
    if (!d) return "";
    if (d.type === "wifi-edit") return this._wifiEditDialog(d);
    if (d.type === "wifi-add") return this._wifiAddDialog(d);
    if (d.type === "wifi-timer") return this._wifiTimerDialog(d);
    if (d.type === "client") return this._clientDialog(d);
    const device = findDevice(this._data, d.device);
    if (!device && d.type !== "confirm") return "";
    if (d.type === "port") return this._portDialog(device, d);
    if (d.type === "reboot") return this._rebootDialog(device);
    if (d.type === "restore") return this._restoreDialog(device);
    if (d.type === "confirm") return this._confirmDialog(d);
    return "";
  }

  _openDialog(dialog) {
    if (!this._dialog) this._lastFocus = this.shadowRoot.activeElement;
    this._dialog = { focus: '.dialog [data-action="close"]', ...dialog };
    this._render();
  }

  _closeDialog() {
    this._dialog = null;
    this._render();
    if (this._lastFocus && this._lastFocus.isConnected) this._lastFocus.focus();
  }

  _toast(message, error = false) {
    clearTimeout(this._toastTimer);
    let el = this.shadowRoot.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.setAttribute("role", "status");
      this.shadowRoot.appendChild(el);
    }
    el.className = `toast ${error ? "err" : ""}`;
    el.textContent = message;
    this._toastTimer = setTimeout(() => el.remove(), error ? 7000 : 3500);
  }

  /* _call("set_port", data, options) for an action, or _call({type: ...}, options) for a WebSocket command. */
  async _call(service, data, options) {
    if (typeof service === "object" && options === undefined) {
      options = data;
      data = undefined;
    }
    const { busyKey = typeof service === "object" ? service.type : service, success = null, hold = false } = options || {};
    if (this._busy.has(busyKey)) return false;
    this._busy.add(busyKey);
    this._render();
    let ok = false;
    try {
      if (typeof service === "object") await this._hass.callWS(service);
      else await this._hass.callService(DOMAIN, service, data);
      if (success) this._toast(success);
      ok = true;
    } catch (err) {
      this._toast((err && err.message) || String(err), true);
    }
    if (ok && hold) {
      this._held = this._held || new Set();
      this._held.add(busyKey);
      setTimeout(() => {
        if (this._held && this._held.delete(busyKey)) {
          this._busy.delete(busyKey);
          this._render();
        }
      }, 8000);
    } else {
      this._busy.delete(busyKey);
    }
    this._render();
    return ok;
  }

  _isBusy(key) {
    return this._busy.has(key);
  }

  /* ---------------- Wi-Fi networks ---------------- */

  _isAdmin() {
    return !this._hass || !this._hass.user || this._hass.user.is_admin !== false;
  }

  _wifiToggle(ssid, on) {
    this._optimisticWifi = this._optimisticWifi || new Map();
    this._optimisticWifi.set(ssid, { on, until: Date.now() + 15000 });
    this._render();
    this._call(
      { type: `${DOMAIN}/wifi/update`, ssid, enabled: on },
      { busyKey: `wifi:${ssid}`, success: `${ssid} turned ${on ? "on" : "off"}` },
    ).then((ok) => {
      if (!ok) this._optimisticWifi.delete(ssid);
      this._render();
    });
  }

  /* The toggle shows the user's choice until the access points report it. */
  _wifiOn(net) {
    const pending = this._optimisticWifi && this._optimisticWifi.get(net.name);
    if (pending && (pending.until < Date.now() || pending.on === net.on)) this._optimisticWifi.delete(net.name);
    else if (pending) return pending.on;
    return net.on;
  }

  _networkCard(net, { compact = false } = {}) {
    const admin = this._isAdmin() && net.configurable;
    const on = this._wifiOn(net);
    const busy = this._isBusy(`wifi:${net.name}`);
    const apNames = net.aps.map((a) => a.device.name);
    return `<div class="card net" data-ssid="${esc(net.name)}">
      <div class="net-head">
        <span class="avatar ${on ? "" : "off"}">${icon("wifi")}</span>
        <div class="grow">
          <div class="net-name">${esc(net.name)} ${net.security ? `<span class="muted" title="${esc(net.security)}">${icon("lock")}</span>` : ""}</div>
          <div class="tile-sub">${esc(wifiStatus({ ...net, on }))}</div>
        </div>
        <label class="toggle" title="${on ? "Turn off" : "Turn on"}">
          <input type="checkbox" data-wifi-toggle="${esc(net.name)}" ${on ? "checked" : ""} ${admin && !busy ? "" : "disabled"} aria-label="${esc(net.name)} on or off">
          <span class="track"></span>
        </label>
      </div>
      ${net.timer ? `<div class="timer-note">${icon("timer")}<span class="grow">${esc(net.name)} goes back ${net.timer.enabled ? "off" : "on"} at ${esc(fmtUntil(net.timer.until))}.</span>
        ${admin ? `<button class="btn" data-action="wifi-timer-restore" data-ssid="${esc(net.name)}">${net.timer.enabled ? "Turn off now" : "Turn on now"}</button>
        <button class="btn" data-action="wifi-timer-keep" data-ssid="${esc(net.name)}">Keep ${net.timer.enabled ? "on" : "off"}</button>` : ""}</div>` : ""}
      ${net.configurable ? "" : `<div class="muted" style="font-size:12px">Listed by the access point, but not one of its configured networks, so it can't be changed here.</div>`}
      ${compact ? "" : `<div class="chips">
        <span class="chip">${icon("clients")}${net.clients} client${net.clients === 1 ? "" : "s"}</span>
        ${net.bands.map((b) => `<span class="chip">${esc(b)}</span>`).join("")}
        ${net.hidden ? '<span class="chip">hidden</span>' : ""}
        ${net.security ? `<span class="chip">${esc(net.security)}</span>` : '<span class="chip warn">open</span>'}
        <span class="chip" title="${esc(apNames.join(", "))}">${net.aps.length === 1 ? esc(apNames[0]) : `${net.aps.length} access points`}</span>
      </div>`}
      ${admin ? `<div class="net-actions">
        <button class="btn" data-action="wifi-timer" data-ssid="${esc(net.name)}">${icon("timer")}${on ? "Turn off for…" : "Turn on for…"}</button>
        <span class="grow"></span>
        <button class="btn" data-action="wifi-edit" data-ssid="${esc(net.name)}">${icon("edit")}Edit</button>
      </div>` : ""}
    </div>`;
  }

  _bandChecks(allBands, selected) {
    return `<div class="checks">${allBands
      .map((b) => `<label><input type="checkbox" data-field="bands" data-multi value="${esc(b)}" ${selected.includes(b) ? "checked" : ""}>${esc(b)}</label>`)
      .join("")}</div>`;
  }

  _apChecks(aps, selected) {
    return `<div class="checks">${aps
      .map((d) => `<label><input type="checkbox" data-field="aps" data-multi value="${esc(d.entry_id)}" ${selected.includes(d.entry_id) ? "checked" : ""}>${esc(d.name)}</label>`)
      .join("")}</div>`;
  }

  _passwordField(form, { required = false } = {}) {
    const show = form.showPassword;
    const value = form.password || "";
    const bad = value && (value.length < 8 || value.length > 63);
    return `<div class="form-row"><label class="section-title" for="wifi-password">${required ? "Password" : "New password"}</label>
      <div class="field">
        <input type="${show ? "text" : "password"}" id="wifi-password" data-field="password" value="${esc(value)}" autocomplete="new-password" spellcheck="false"
          placeholder="${required ? "8–63 characters, empty for an open network" : "Leave empty to keep the current one"}">
        <button class="icon-btn" data-action="wifi-show-password" aria-label="${show ? "Hide" : "Show"} password" title="${show ? "Hide" : "Show"} password">${icon("eye")}</button>
      </div>
      <div class="hint ${bad ? "err" : ""}">${bad ? "A Wi-Fi password needs 8 to 63 characters." : required && !value ? "Without a password anyone nearby can join." : "Sent straight to the access points; never stored in Home Assistant."}</div>
    </div>`;
  }

  _wifiEditDialog(d) {
    const { networks, allBands } = wifiNetworks(this._data);
    const net = networks.find((n) => n.name === d.ssid);
    if (!net) return `<div class="dialog" role="dialog" aria-modal="true"><p>${esc(d.ssid)} no longer exists.</p><div class="dlg-actions"><button class="btn" data-action="close">Close</button></div></div>`;
    const form = (d.form = d.form || {});
    const editable = net.aps.filter((a) => a.ssid.configurable !== false);
    const scope = d.scope ? editable.filter((a) => d.scope.includes(a.device.entry_id)) : editable;
    const currentBands = [...new Set(scope.flatMap((a) => a.bands))].sort();
    const bands = form.bands ?? currentBands;
    const hidden = form.hidden ?? scope.some((a) => a.ssid.hidden);
    const maxClients = form.max ?? (net.maxClients ?? "");
    const aps = form.aps ?? scope.map((a) => a.device.entry_id);
    const busy = this._isBusy(`wifi-save:${net.name}`);
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      <div class="dlg-head"><div class="grow"><h2 id="dlg-title">${esc(net.name)}</h2>
        <div class="muted">${esc(wifiStatus(net))}${net.security ? ` · ${esc(net.security)}` : ""}</div></div>
        <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
      ${this._passwordField(form)}
      <div class="form-row"><span class="section-title">Bands</span>${this._bandChecks(allBands, bands)}
        <div class="hint">No band ticked turns the network off.</div></div>
      <div class="form-row"><label class="toggle"><input type="checkbox" data-field="hidden" ${hidden ? "checked" : ""}><span class="track"></span><span>Hide the network name</span></label></div>
      ${net.maxClients != null ? `<div class="form-row"><label class="section-title" for="wifi-max">Max clients per access point</label>
        <div class="field"><input type="number" id="wifi-max" min="1" max="255" data-field="max" value="${esc(maxClients)}"></div></div>` : ""}
      ${editable.length > 1 ? `<div class="form-row"><span class="section-title">Apply to</span>${this._apChecks(editable.map((a) => a.device), aps)}</div>` : ""}
      <div class="dlg-actions">
        <button class="btn danger" data-action="wifi-delete" style="margin-right:auto">${icon("trash")}Delete</button>
        <button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="wifi-save" ${busy ? "disabled" : ""}>${icon("check")}${busy ? "Saving…" : "Save"}</button>
      </div></div>`;
  }

  _wifiAddDialog(d) {
    const { aps, networks, allBands } = wifiNetworks(this._data);
    const form = (d.form = d.form || {});
    const name = form.name ?? "";
    const exists = networks.some((n) => n.name === name.trim());
    const busy = this._isBusy("wifi-create");
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      <div class="dlg-head"><h2 id="dlg-title" class="grow">Add a Wi-Fi network</h2>
        <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
      <div class="form-row" style="margin-top:0"><label class="section-title" for="wifi-name">Network name</label>
        <div class="field"><input type="text" id="wifi-name" data-field="name" maxlength="32" value="${esc(name)}" autocomplete="off" spellcheck="false" placeholder="e.g. Guest Wifi"></div>
        ${exists ? '<div class="hint err">A network with this name already exists.</div>' : ""}</div>
      ${this._passwordField(form, { required: true })}
      <div class="form-row"><span class="section-title">Bands</span>${this._bandChecks(allBands, form.bands ?? allBands)}</div>
      <div class="form-row"><label class="toggle"><input type="checkbox" data-field="hidden" ${form.hidden ? "checked" : ""}><span class="track"></span><span>Hide the network name</span></label></div>
      ${aps.length > 1 ? `<div class="form-row"><span class="section-title">Access points</span>${this._apChecks(aps, form.aps ?? aps.map((a) => a.entry_id))}</div>` : ""}
      <div class="dlg-actions">
        <button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="wifi-create" ${busy ? "disabled" : ""}>${icon("plus")}${busy ? "Adding…" : "Add network"}</button>
      </div></div>`;
  }

  _wifiTimerDialog(d) {
    const { networks } = wifiNetworks(this._data);
    const net = networks.find((n) => n.name === d.ssid);
    if (!net) return "";
    const turnOn = d.turnOn ?? !this._wifiOn(net);
    const form = (d.form = d.form || {});
    const minutes = Number(form.minutes ?? 60);
    const busy = this._isBusy(`wifi-timer:${net.name}`);
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      <div class="dlg-head"><h2 id="dlg-title" class="grow">Turn ${esc(net.name)} ${turnOn ? "on" : "off"} for…</h2>
        <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
      <p class="muted" style="margin:0 0 12px">Afterwards it goes back to how it is now, even if Home Assistant restarts in between.</p>
      <div class="durations" role="radiogroup">${WIFI_DURATIONS.map(
        ([m, label]) => `<label class="choice"><input type="radio" name="duration" data-field="minutes" value="${m}" ${m === minutes ? "checked" : ""}><span>${label}</span></label>`,
      ).join("")}</div>
      <div class="dlg-actions">
        <button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="wifi-timer-start" ${busy ? "disabled" : ""}>${icon("timer")}${busy ? "Working…" : `Turn ${turnOn ? "on" : "off"}`}</button>
      </div></div>`;
  }

  _wifiAction(action, el) {
    const d = this._dialog;
    const ssid = el.dataset.ssid || (d && d.ssid);
    switch (action) {
      case "wifi-add":
        this._openDialog({ type: "wifi-add", focus: "#wifi-name" });
        return true;
      case "wifi-edit":
        this._openDialog({ type: "wifi-edit", ssid, scope: el.dataset.scope ? [el.dataset.scope] : null });
        return true;
      case "wifi-timer":
        this._openDialog({ type: "wifi-timer", ssid });
        return true;
      case "wifi-show-password":
        d.form = d.form || {};
        d.form.showPassword = !d.form.showPassword;
        this._render();
        return true;
      case "wifi-timer-restore":
      case "wifi-timer-keep":
        this._call(
          { type: `${DOMAIN}/wifi/timer_cancel`, ssid, restore: action === "wifi-timer-restore" },
          { busyKey: `wifi:${ssid}`, success: action === "wifi-timer-restore" ? `${ssid} is back to how it was` : `Timer for ${ssid} cancelled` },
        );
        return true;
      case "wifi-timer-start": {
        const { networks } = wifiNetworks(this._data);
        const net = networks.find((n) => n.name === ssid);
        if (!net) return true;
        const turnOn = d.turnOn ?? !this._wifiOn(net);
        const minutes = Number((d.form && d.form.minutes) ?? 60);
        const label = (WIFI_DURATIONS.find(([m]) => m === minutes) || [0, `${minutes} minutes`])[1];
        this._call(
          { type: `${DOMAIN}/wifi/update`, ssid, enabled: turnOn, duration_minutes: minutes },
          { busyKey: `wifi-timer:${ssid}`, success: `${ssid} is ${turnOn ? "on" : "off"} for ${label}` },
        ).then((ok) => ok && this._closeDialog());
        return true;
      }
      case "wifi-save": {
        const { networks } = wifiNetworks(this._data);
        const net = networks.find((n) => n.name === ssid);
        if (!net) return true;
        const form = d.form || {};
        const editable = net.aps.filter((a) => a.ssid.configurable !== false);
        const scope = d.scope ? editable.filter((a) => d.scope.includes(a.device.entry_id)) : editable;
        const msg = { type: `${DOMAIN}/wifi/update`, ssid };
        const aps = form.aps ?? scope.map((a) => a.device.entry_id);
        if (!aps.length) return this._toast("Choose at least one access point", true), true;
        if (aps.length !== editable.length || editable.length !== net.aps.length) msg.entry_ids = aps;
        if (form.password) {
          if (form.password.length < 8 || form.password.length > 63) return this._toast("A Wi-Fi password needs 8 to 63 characters", true), true;
          msg.password = form.password;
        }
        const currentBands = [...new Set(scope.flatMap((a) => a.bands))].sort();
        if (form.bands && form.bands.slice().sort().join() !== currentBands.join()) msg.bands = form.bands;
        if (form.hidden !== undefined && form.hidden !== scope.some((a) => a.ssid.hidden)) msg.hidden = form.hidden;
        if (form.max !== undefined && form.max !== "" && Number(form.max) !== net.maxClients) msg.max_clients = Number(form.max);
        if (Object.keys(msg).length === 2 || (Object.keys(msg).length === 3 && msg.entry_ids)) {
          this._closeDialog();
          return true;
        }
        this._call(msg, { busyKey: `wifi-save:${ssid}`, success: `${ssid} saved` }).then((ok) => ok && this._closeDialog());
        return true;
      }
      case "wifi-create": {
        const { aps, networks, allBands } = wifiNetworks(this._data);
        const form = d.form || {};
        const name = (form.name || "").trim();
        if (!name) return this._toast("Enter a network name", true), true;
        if (networks.some((n) => n.name === name)) return this._toast("A network with this name already exists", true), true;
        if (form.password && (form.password.length < 8 || form.password.length > 63)) return this._toast("A Wi-Fi password needs 8 to 63 characters", true), true;
        const bands = form.bands ?? allBands;
        if (!bands.length) return this._toast("Choose at least one band", true), true;
        const chosen = form.aps ?? aps.map((a) => a.entry_id);
        if (!chosen.length) return this._toast("Choose at least one access point", true), true;
        const msg = { type: `${DOMAIN}/wifi/create`, ssid: name, bands, hidden: Boolean(form.hidden) };
        if (form.password) msg.password = form.password;
        if (chosen.length !== aps.length) msg.entry_ids = chosen;
        this._call(msg, { busyKey: "wifi-create", success: `${name} added${form.password ? "" : " (open network)"}` }).then((ok) => ok && this._closeDialog());
        return true;
      }
      case "wifi-delete": {
        const back = { ...d };
        this._dialog = {
          type: "confirm",
          title: `Delete ${ssid}?`,
          text: `The network is removed from ${d.scope ? "this access point" : "every access point that has it"}, and everyone on it is disconnected.`,
          confirm: "Delete",
          danger: true,
          back,
          focus: '.dialog [data-action="close"]',
          onYes: () =>
            this._call(
              { type: `${DOMAIN}/wifi/delete`, ssid, ...(d.scope ? { entry_ids: d.scope } : {}) },
              { busyKey: `wifi:${ssid}`, success: `${ssid} deleted` },
            ).then((ok) => ok && this._closeDialog()),
        };
        this._render();
        return true;
      }
      default:
        return false;
    }
  }

  /* ---------------- dialogs ---------------- */

  _clientDialog(d) {
    const info = clientInfo(this._data, d.mac);
    const admin = this._isAdmin();
    const w = info.wifi;
    const wired = info.wired;
    const ha = info.ha_device;
    const row = (label, value) => (value ? `<dt>${esc(label)}</dt><dd>${value}</dd>` : "");
    const form = (d.form = d.form || {});
    const busyName = this._isBusy(`client-name:${d.mac}`);
    const quality = w && w.sta.signal != null ? (w.sta.signal >= -55 ? "excellent" : w.sta.signal >= -65 ? "good" : w.sta.signal >= -75 ? "fair" : "weak") : null;
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      <div class="dlg-head"><div class="grow"><h2 id="dlg-title">${esc(clientTitle(info))}</h2>
        <div class="muted">${esc([info.kind ? KIND_LABELS[info.kind] : null, w ? "Wi-Fi" : wired ? "Wired" : "Not connected now"].filter(Boolean).join(" · "))}</div></div>
        <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
      <section><dl class="kv">
        ${row("MAC address", `<span class="mono">${esc(info.mac)}</span>${info.private ? '<br><span class="muted">Private (randomised) address: phones and laptops make one up per network, so the maker is unknown and it can change.</span>' : ""}`)}
        ${row("Maker", info.vendor ? esc(info.vendor) : "")}
        ${row("IP address", info.ip ? `<span class="mono">${esc(info.ip)}</span>` : "")}
        ${row("Hostname", info.hostname && info.hostname !== info.name ? esc(info.hostname) : "")}
        ${w ? row("Connected to", `<strong>${esc(w.device.name)}</strong> · ${esc(w.sta.ssid || "Wi-Fi")}${w.radio && w.radio.band ? ` · ${esc(w.radio.band)}` : ""}${w.sta.channel ? ` · channel ${esc(w.sta.channel)}` : ""}`) : ""}
        ${w && w.sta.signal != null ? row("Signal", `${signalBars(w.sta.signal)}${esc(w.sta.signal)} dBm (${quality})${w.sta.snr != null ? ` · SNR ${esc(w.sta.snr)} dB` : ""}`) : ""}
        ${w && (w.sta.tx_rate || w.sta.rx_rate) ? row("Speed", `↓ ${esc(w.sta.tx_rate || "?")} &nbsp; ↑ ${esc(w.sta.rx_rate || "?")}`) : ""}
        ${w ? row("Connected for", w.sta.connected ? esc(w.sta.connected) : "") : ""}
        ${w ? row("Security", w.sta.security ? esc(w.sta.security) : "") : ""}
        ${w && w.sta.vlan ? row("VLAN", esc(w.sta.vlan)) : ""}
        ${wired && !wired.port ? row("Connected to", `<strong>${esc(wired.device.name)}</strong> · LAN`) : ""}
        ${wired && wired.port ? row("Connected to", `<strong>${esc(wired.device.name)}</strong> · port ${esc(wired.port.name)}${wired.port.label ? ` (${esc(wired.port.label)})` : ""}${wired.port.speed ? ` · ${esc(wired.port.speed >= 1000 ? `${wired.port.speed / 1000} Gbit/s` : `${wired.port.speed} Mbit/s`)}` : ""}`) : ""}
        ${wired && wired.vlan != null ? row("VLAN", esc(wired.vlan)) : ""}
        ${ha ? row("In Home Assistant", `<a href="/config/devices/device/${esc(ha.id)}" target="_top">${esc(ha.name || "Device")}</a>${[ha.manufacturer, ha.model].filter(Boolean).length ? `<br><span class="muted">${esc([ha.manufacturer, ha.model].filter(Boolean).join(" "))}</span>` : ""}${ha.area ? `<br><span class="muted">${esc(ha.area)}</span>` : ""}`) : ""}
        ${info.key ? row("Wi-Fi key", `${esc(info.key.name)}${info.key.wifi ? ` <span class="muted">on ${esc(info.key.wifi)}</span>` : ""}`) : ""}
        ${info.name_source && info.name_source !== "nickname" ? row("Name from", esc({ home_assistant: "Home Assistant", key: "its Wi-Fi key", onboarding: "the onboarding portal", hostname: "its hostname (DHCP)" }[info.name_source] || info.name_source)) : ""}
      </dl></section>
      ${admin ? `<section><label class="section-title" for="client-nick">Nickname</label>
        <div class="field"><input type="text" id="client-nick" maxlength="64" data-field="nickname" value="${esc(form.nickname ?? info.nickname ?? "")}" placeholder="${esc(info.name && info.name_source !== "nickname" ? info.name : "e.g. Kitchen tablet")}" data-enter="client-rename" autocomplete="off">
          <button class="btn" data-action="client-rename" ${busyName ? "disabled" : ""}>${icon("edit")}Save</button></div>
        <div class="muted" style="font-size:12px;margin-top:6px">Shown everywhere in the panel instead of the MAC address. Leave empty to remove.</div></section>` : ""}
      <div class="dlg-actions">
        ${wired && wired.port ? `<button class="btn" data-action="client-port" data-device="${esc(wired.device.entry_id)}" data-port="${esc(wired.port.name)}">${icon("switch")}Open port ${esc(wired.port.name)}</button>` : ""}
        ${w && admin ? `<button class="btn danger" data-action="client-disconnect">${icon("close")}Disconnect</button>` : ""}
        <button class="btn primary" data-action="close">Done</button></div></div>`;
  }

  _portSpeedSection(device, port, d, busy) {
    const options = port.speed_options || [];
    if (!(device.features && device.features.port_speed) || !options.length) return "";
    const setting = port.speed_setting || { speed: "auto", duplex: "auto" };
    const speed = d.speed ?? setting.speed;
    const duplexes = speed === "auto" ? ["auto"] : speed === "10" || speed === "100" ? ["full", "half"] : ["full"];
    const duplex = duplexes.includes(d.duplex ?? setting.duplex) ? d.duplex ?? setting.duplex : duplexes[0];
    const label = (s) => (s === "auto" ? "Auto (negotiate)" : s === "1000" ? "1 Gbit/s" : s === "10000" ? "10 Gbit/s" : `${s} Mbit/s`);
    const now = setting.speed === "auto" ? "Auto" : `${label(setting.speed)} ${setting.duplex}`;
    const changed = speed !== setting.speed || duplex !== setting.duplex;
    return `<section>
        <label class="section-title" for="port-speed">Speed</label>
        <div class="field speed-row">
          <select id="port-speed" class="port-select" data-speed-field="speed">${options.map((o) => `<option value="${o}" ${o === speed ? "selected" : ""}>${label(o)}</option>`).join("")}</select>
          <select id="port-duplex" class="port-select" data-speed-field="duplex" aria-label="Duplex" ${duplexes.length === 1 ? "disabled" : ""}>${duplexes.map((x) => `<option value="${x}" ${x === duplex ? "selected" : ""}>${x === "auto" ? "Auto" : x === "full" ? "Full duplex" : "Half duplex"}</option>`).join("")}</select>
          <button class="btn" data-action="port-speed" ${busy("speed") || !changed ? "disabled" : ""}>${icon("check")}${busy("speed") ? "Setting…" : "Apply"}</button>
        </div>
        <div class="muted" style="font-size:12px;margin-top:6px">Set to ${esc(now)}${port.up && port.speed ? ` · link at ${esc(label(String(port.speed)))}` : ""}. Both ends must match: use Auto unless the other device needs a fixed speed.${device.cli === "exos" ? "" : " The commands for this device are assumed (not yet confirmed on one)."}</div>
      </section>`;
  }

  _portDialog(device, d) {
    const port = (device.ports || []).find((p) => p.name === d.port);
    if (!port) return "";
    const names = clientNames(this._data);
    const key = `${device.entry_id}:${port.name}`;
    const busy = (what) => this._isBusy(`${what}:${key}`);
    const poe = port.poe;
    const neighbor = port.neighbor;
    const clients = port.clients || [];
    const status = portStatusText(port);
    return `
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div class="dlg-head">
          <div class="grow">
            <h2 id="dlg-title">Port ${esc(port.name)}${port.label ? ` <span class="chip">${esc(port.label)}</span>` : ""}</h2>
            <div class="muted">${esc(device.name)} · ${esc(status)}${port.vlan ? ` · VLAN ${esc(port.vlan)}` : ""}</div>
          </div>
          <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button>
        </div>
        <section>
          <div class="controls">
            ${device.features && device.features.port_control === false ? `<span class="muted">${esc(portStatusText(port))}${port.uplink ? " · WAN" : ""}</span>` : `<label class="toggle">
              <input type="checkbox" data-toggle="port" ${port.enabled !== false ? "checked" : ""} ${port.present === false ? "disabled" : ""}>
              <span class="track"></span><span>Port ${port.enabled !== false ? "on" : "off"}</span>
            </label>`}
            ${
              poe
                ? `<label class="toggle">
                    <input type="checkbox" data-toggle="poe" ${poe.enabled !== false ? "checked" : ""}>
                    <span class="track"></span><span>PoE ${poe.enabled !== false ? "on" : "off"}</span>
                  </label>
                  <button class="btn" data-action="poe-cycle" ${busy("cycle") || poe.enabled === false ? "disabled" : ""}>${icon("cycle")}${busy("cycle") ? "Restarting…" : "Restart PoE device"}</button>`
                : ""
            }
          </div>
          ${port.uplink && !(device.features && device.features.port_control === false) ? `<p class="muted" style="margin:10px 0 0">${icon("up")} Uplink port: turning it off can disconnect Home Assistant from the switch.</p>` : ""}
        </section>
        ${
          device.features && device.features.port_names
            ? `<section>
                <label class="section-title" for="port-name">Port name</label>
                <div class="field">
                  <input type="text" id="port-name" maxlength="15" value="${esc(port.label || "")}" placeholder="e.g. camera-1" data-enter="rename" autocomplete="off" spellcheck="false">
                  <button class="btn" data-action="rename" ${busy("rename") ? "disabled" : ""}>${icon("edit")}Save</button>
                </div>
                <div class="muted" style="font-size:12px;margin-top:6px">Up to 15 letters, digits, - _ or . (no spaces). Leave empty to clear.</div>
              </section>`
            : ""
        }
        ${this._portSpeedSection(device, port, d, busy)}
        <section>
          <dl class="kv">
            ${neighbor ? `<dt>Connected to</dt><dd><strong>${esc(neighbor.name || "Unknown")}</strong>${neighbor.port ? ` <span class="muted">port ${esc(neighbor.port)}</span>` : ""}${neighbor.address ? `<br><span class="mono">${esc(neighbor.address)}</span>` : ""}</dd>` : ""}
            ${port.media ? `<dt>Port type</dt><dd>${esc(mediaText(port).replace(/^./, (c) => c.toUpperCase()))}</dd>` : ""}
            ${poe ? `<dt>PoE</dt><dd>${poe.delivering ? `<strong>${esc(poe.power_w ?? 0)} W</strong>` : esc(poe.status || "Off")}${poe.class ? ` <span class="muted">${esc(poe.class)}</span>` : ""}</dd>` : ""}
            <dt>Traffic</dt><dd class="num">↓ ${fmtRate(port.rx_kbps)} &nbsp; ↑ ${fmtRate(port.tx_kbps)}</dd>
            <dt>Total</dt><dd class="num">↓ ${fmtBytes(port.rx_bytes)} &nbsp; ↑ ${fmtBytes(port.tx_bytes)}</dd>
            ${port.errors ? `<dt>Errors</dt><dd class="num" style="color:var(--ah-err)">${esc(port.errors)}</dd>` : ""}
          </dl>
        </section>
        <section>
          <div class="section-title">Devices on this port (${clients.length})</div>
          ${
            clients.length
              ? `<ul class="client-list">${clients
                  .slice(0, 50)
                  .map(
                    (c) =>
                      `<li><button class="link-btn" data-action="client-open" data-mac="${esc(c.mac)}">${(() => {
                        const info = (this._data.clients || {})[c.mac] || {};
                        const label = names.get(c.mac);
                        const sub = info.vendor || (info.private ? "Private address" : "");
                        return `${label ? `${esc(label)} ` : ""}<span class="muted mono">${esc(c.mac)}</span>${sub ? ` <span class="muted">${esc(sub)}</span>` : ""}`;
                      })()}</button><span class="muted">${c.vlan != null ? `VLAN ${esc(c.vlan)}` : ""}${(this._data.clients || {})[c.mac]?.ip ? ` ${esc(this._data.clients[c.mac].ip)}` : ""}</span></li>`,
                  )
                  .join("")}</ul>${clients.length > 50 ? `<div class="muted">and ${clients.length - 50} more</div>` : ""}`
              : `<div class="muted">None learned.</div>`
          }
        </section>
      </div>`;
  }

  _rebootDialog(device) {
    const unsaved = device.config && device.config.unsaved;
    const busy = this._isBusy(`reboot:${device.entry_id}`);
    const choice = this._dialog.save ?? "save";
    return `
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div class="dlg-head"><h2 id="dlg-title" class="grow">Reboot ${esc(device.name)}?</h2>
          <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
        <p class="muted" style="margin:0">The device is unreachable for a few minutes, and everything connected through it loses its connection.</p>
        ${
          unsaved
            ? `<p style="margin:14px 0 0"><span class="chip warn">${icon("warn")}Unsaved changes</span></p>
               <label class="choice"><input type="radio" name="save" value="save" ${choice === "save" ? "checked" : ""} data-choice="save">
                 <span><strong>Save, then reboot</strong><br><span class="muted">Keep the changes.</span></span></label>
               <label class="choice"><input type="radio" name="save" value="discard" ${choice === "discard" ? "checked" : ""} data-choice="save">
                 <span><strong>Reboot without saving</strong><br><span class="muted">The changes are lost; the switch starts with its saved configuration.</span></span></label>`
            : ""
        }
        <div class="dlg-actions">
          <button class="btn" data-action="close">Cancel</button>
          <button class="btn primary" data-action="reboot-confirm" ${busy ? "disabled" : ""}>${icon("reboot")}${busy ? "Rebooting…" : "Reboot"}</button>
        </div>
      </div>`;
  }

  _restoreDialog(device) {
    const busy = this._isBusy(`restore:${device.entry_id}`);
    const summary = device.config && device.config.change_summary;
    return `
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div class="dlg-head"><h2 id="dlg-title" class="grow">Restore the last good configuration?</h2>
          <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>
        <p class="muted" style="margin:0">The latest backup of ${esc(device.name)} is sent back to it line by line, then saved on the device.
          ${summary ? `It differs from the current configuration by ${esc(summary.added)} added and ${esc(summary.removed)} removed lines.` : ""}</p>
        ${
          summary && summary.sample && summary.sample.length
            ? `<pre class="mono" style="background:var(--ah-bg);padding:10px;border-radius:8px;overflow:auto;max-height:180px;margin:12px 0 0">${esc(summary.sample.join("\n"))}</pre>`
            : ""
        }
        <div class="dlg-actions">
          <button class="btn" data-action="close">Cancel</button>
          <button class="btn primary" data-action="restore-confirm" ${busy ? "disabled" : ""}>${icon("restore")}${busy ? "Restoring…" : "Restore and save"}</button>
        </div>
      </div>`;
  }

  _confirmDialog(d) {
    return `
      <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlg-title">
        <h2 id="dlg-title">${esc(d.title)}</h2>
        <p class="muted">${esc(d.text)}</p>
        <div class="dlg-actions">
          <button class="btn" data-action="close">Cancel</button>
          <button class="btn primary ${d.danger ? "danger" : ""}" data-action="confirm-yes">${esc(d.confirm || "OK")}</button>
        </div>
      </div>`;
  }

  /* ---------------- events ---------------- */

  _onClick(ev) {
    const portEl = ev.target.closest("[data-port]");
    if (portEl && !portEl.closest(".dialog")) {
      const device = portEl.closest("[data-device]")?.dataset.device || this._defaultDevice();
      this._onPortClick(device, portEl.dataset.port);
      return;
    }
    const actionEl = ev.target.closest("[data-action]");
    if (!actionEl) return;
    const action = actionEl.dataset.action;
    if (action === "scrim") {
      if (ev.target === actionEl) this._closeDialog();
      return;
    }
    this._handleAction(action, actionEl);
  }

  _onPortClick(deviceRef, port) {
    this._openDialog({ type: "port", device: deviceRef, port });
  }

  _defaultDevice() {
    return null;
  }

  _captureField(target) {
    if (!this._dialog || !target.dataset || !target.dataset.field) return false;
    const form = (this._dialog.form = this._dialog.form || {});
    const field = target.dataset.field;
    if (target.type === "checkbox" && target.dataset.multi !== undefined) {
      const root = target.closest(".dialog");
      form[field] = [...root.querySelectorAll(`input[data-field="${field}"]`)].filter((i) => i.checked).map((i) => i.value);
    } else if (target.type === "checkbox") form[field] = target.checked;
    else form[field] = target.value;
    return true;
  }

  _onChange(ev) {
    const target = ev.target;
    if (this._captureField(target)) {
      if (target.dataset.rerender !== undefined) {
        this._forceDialog = true; // the user changed a control that reshapes the dialog: redraw even if a text field has focus
        this._render();
      }
      return;
    }
    if (target.dataset.wifiToggle !== undefined) {
      this._wifiToggle(target.dataset.wifiToggle, target.checked);
      return;
    }
    if (target.dataset.speedField && this._dialog) {
      this._dialog[target.dataset.speedField] = target.value;
      if (target.dataset.speedField === "speed") this._dialog.duplex = undefined;
      this._render();
      return;
    }
    if (target.dataset.choice && this._dialog) {
      this._dialog[target.dataset.choice] = target.value;
      return;
    }
    if (!target.dataset.toggle || !this._dialog || this._dialog.type !== "port") return;
    const device = findDevice(this._data, this._dialog.device);
    const port = device && device.ports.find((p) => p.name === this._dialog.port);
    if (!port) return;
    const key = `${device.entry_id}:${port.name}`;
    if (target.dataset.toggle === "port") {
      const enabled = target.checked;
      if (!enabled && port.uplink) {
        target.checked = true;
        this._dialog = {
          type: "confirm",
          title: `Turn off uplink port ${port.name}?`,
          text: `Port ${port.name} leads to another switch or router. Turning it off can cut Home Assistant off from ${device.name}, and you may have to turn it back on from the console.`,
          confirm: "Turn off",
          danger: true,
          onYes: () => this._toggle(device, port, "port", false, "set_port", { force: true }),
          back: { type: "port", device: this._dialog.device, port: port.name },
          focus: '.dialog [data-action="close"]',
        };
        this._render();
        return;
      }
      this._toggle(device, port, "port", enabled, "set_port");
    } else if (target.dataset.toggle === "poe") {
      this._toggle(device, port, "poe", target.checked, "set_poe");
    }
  }

  /* Flip at once; Home Assistant updates the real state within a second or two. */
  async _toggle(device, port, field, value, service, extra = {}) {
    const key = `${field}:${device.entry_id}:${port.name}`;
    this._setOptimistic(device, port.name, field, value);
    this._queued = this._queued || new Map();
    if (this._busy.has(key)) {
      // A command for this switch is still running: send the latest choice right after it.
      this._queued.set(key, { value, service, extra });
      return;
    }
    const ok = await this._call(service, { device_id: device.device_id, port: port.name, enabled: value, ...extra }, { busyKey: key });
    const next = this._queued.get(key);
    this._queued.delete(key);
    if (next && next.value !== value) {
      this._toggle(device, port, field, next.value, next.service, next.extra);
    } else if (!ok) {
      this._clearOptimistic(device, port.name, field);
    }
  }

  _handleAction(action, el) {
    if (action.startsWith("wifi-") && this._wifiAction(action, el)) return;
    const dialog = this._dialog;
    const device = dialog ? findDevice(this._data, dialog.device) : findDevice(this._data, el.closest("[data-device]")?.dataset.device);
    switch (action) {
      case "close":
        if (dialog && dialog.back) {
          this._dialog = dialog.back;
          this._render();
        } else this._closeDialog();
        break;
      case "confirm-yes": {
        const back = dialog.back;
        this._dialog = back || null;
        this._render();
        dialog.onYes();
        break;
      }
      case "poe-cycle": {
        const key = `${device.entry_id}:${dialog.port}`;
        this._call("power_cycle_poe", { device_id: device.device_id, port: dialog.port }, { busyKey: `cycle:${key}`, success: `PoE on port ${dialog.port} restarted` });
        break;
      }
      case "client-open":
        this._dialog = { type: "client", mac: el.dataset.mac, back: dialog ? { ...dialog } : null, focus: '.dialog [data-action="close"]' };
        this._render();
        break;
      case "client-port":
        this._dialog = { type: "port", device: el.dataset.device, port: el.dataset.port, focus: '.dialog [data-action="close"]' };
        if (this._navigate) this._navigate(el.dataset.device);
        this._render();
        break;
      case "client-rename": {
        const input = this.shadowRoot.querySelector("#client-nick");
        const name = (input ? input.value : "").trim();
        if (input) input.blur();
        this._call({ type: `${DOMAIN}/client/rename`, mac: dialog.mac, name }, { busyKey: `client-name:${dialog.mac}`, success: name ? `Named ${name}` : "Nickname removed" }).then((ok) => {
          if (ok && this._dialog) this._dialog.form = {};
        });
        break;
      }
      case "client-disconnect": {
        const info = clientInfo(this._data, dialog.mac);
        if (!info.wifi) break;
        const back = { ...dialog };
        this._dialog = {
          type: "confirm",
          title: `Disconnect ${clientTitle(info)}?`,
          text: "It's dropped from the Wi-Fi now. It can join again straight away unless its key is revoked.",
          confirm: "Disconnect",
          danger: true,
          back,
          onYes: () => this._call("disconnect_client", { device_id: info.wifi.device.device_id, mac: dialog.mac }, { busyKey: `disc:${dialog.mac}`, success: `${clientTitle(info)} disconnected` }),
          focus: '.dialog [data-action="close"]',
        };
        this._render();
        break;
      }
      case "port-speed": {
        const port = device.ports.find((p) => p.name === dialog.port);
        const setting = port.speed_setting || { speed: "auto", duplex: "auto" };
        const speed = dialog.speed ?? setting.speed;
        const duplex = speed === "auto" ? "auto" : dialog.duplex ?? (setting.speed === speed ? setting.duplex : "full");
        const key = `${device.entry_id}:${port.name}`;
        const send = (force) =>
          this._call("set_port_speed", { device_id: device.device_id, port: port.name, speed, duplex, ...(force ? { force: true } : {}) },
            { busyKey: `speed:${key}`, hold: true, success: `Port ${port.name} set to ${speed === "auto" ? "Auto" : `${speed === "1000" ? "1 Gbit/s" : speed === "10000" ? "10 Gbit/s" : `${speed} Mbit/s`} ${duplex}`}` });
        if (port.uplink && speed !== "auto") {
          this._dialog = {
            type: "confirm",
            title: `Change the speed of uplink port ${port.name}?`,
            text: `Port ${port.name} leads to another switch or router. If the other side doesn't match, the link drops and Home Assistant can lose ${device.name}; you may need the console to set it back.`,
            confirm: "Change speed",
            danger: true,
            back: { ...dialog },
            onYes: () => send(true),
            focus: '.dialog [data-action="close"]',
          };
          this._render();
        } else send(false);
        break;
      }
      case "rename": {
        const input = this.shadowRoot.querySelector("#port-name");
        const key = `${device.entry_id}:${dialog.port}`;
        const description = (input ? input.value : "").trim();
        if (input) input.blur();
        this._call("set_port_description", { device_id: device.device_id, port: dialog.port, description }, { busyKey: `rename:${key}`, hold: true, success: description ? `Port ${dialog.port} renamed to ${description}` : `Name of port ${dialog.port} cleared` });
        break;
      }
      case "refresh":
        if (device) this._call("refresh", { device_id: device.device_id }, { busyKey: `refresh:${device.entry_id}`, success: `${device.name} refreshed` });
        else this._call("refresh", {}, { busyKey: "refresh:all", success: "All devices refreshed" });
        break;
      case "save":
        this._call("save_config", { device_id: device.device_id }, { busyKey: `save:${device.entry_id}`, success: `Configuration of ${device.name} saved` });
        break;
      case "backup":
        this._call("backup_config", { device_id: device.device_id }, { busyKey: `backup:${device.entry_id}`, success: `Backup of ${device.name} taken` });
        break;
      case "accept":
        this._openDialog({
          type: "confirm",
          device: device.entry_id,
          title: "Keep the current configuration?",
          text: `The configuration ${device.name} is running now becomes the new known-good backup, and the alert is cleared. Do this if the change was intended.`,
          confirm: "Keep current",
          onYes: () => this._call("accept_config", { device_id: device.device_id }, { busyKey: `accept:${device.entry_id}`, success: "Current configuration accepted" }),
        });
        break;
      case "restore":
        this._openDialog({ type: "restore", device: device.entry_id });
        break;
      case "restore-confirm":
        this._call("restore_config", { device_id: device.device_id, confirm: true, save: true }, { busyKey: `restore:${device.entry_id}`, success: `Configuration of ${device.name} restored` }).then(
          (ok) => ok && this._closeDialog(),
        );
        break;
      case "reboot":
        this._openDialog({ type: "reboot", device: device.entry_id, save: "save" });
        break;
      case "reboot-confirm": {
        const data = { device_id: device.device_id, confirm: true };
        if (device.config && device.config.unsaved) data.save_changes = (dialog.save ?? "save") === "save";
        this._call("reboot", data, { busyKey: `reboot:${device.entry_id}`, success: `${device.name} is rebooting` }).then((ok) => ok && this._closeDialog());
        break;
      }
      default:
        this._onAction(action, el, device);
    }
  }

  _onAction() {}

  _render() {}
}

/* ------------------------------------------------------------------ */
/* Device detail (used by the panel)                                    */
/* ------------------------------------------------------------------ */

function deviceActions(base, device) {
  const id = device.entry_id;
  const cfg = device.config || {};
  const busy = (k) => base._isBusy(`${k}:${id}`);
  return `
    <div class="actions">
      <button class="btn" data-action="refresh" ${busy("refresh") ? "disabled" : ""} title="Read everything from the device now">${icon("reboot", busy("refresh") ? "spin" : "")}${busy("refresh") ? "Refreshing…" : "Refresh"}</button>
      ${device.features && device.features.save === false ? "" : `<button class="btn ${cfg.unsaved ? "attention" : ""}" data-action="save" ${busy("save") || !device.online ? "disabled" : ""}>${icon("save")}${busy("save") ? "Saving…" : "Save configuration"}</button>`}
      ${device.features && device.features.backup === false ? "" : `<button class="btn" data-action="backup" ${busy("backup") || !device.online || cfg.changed_after_reboot ? "disabled" : ""}>${icon("backup")}${busy("backup") ? "Backing up…" : "Back up now"}</button>`}
      <button class="btn danger" data-action="reboot" ${!device.online ? "disabled" : ""}>${icon("reboot")}Reboot</button>
    </div>`;
}

function deviceBanners(device) {
  const cfg = device.config || {};
  const out = [];
  if (!device.online) {
    out.push(`<div class="banner err">${icon("warn")}<div class="grow"><strong>${esc(device.name)} is offline.</strong> Home Assistant can't reach it over SSH right now.</div></div>`);
  }
  if (cfg.changed_after_reboot) {
    const s = cfg.change_summary;
    out.push(`<div class="banner err">${icon("warn")}<div class="grow"><strong>Configuration changed after a reboot.</strong>
      This is what a power cut does to unsaved or damaged configurations${s ? ` (${esc(s.added)} lines added, ${esc(s.removed)} removed)` : ""}.
      <div class="banner-actions">${device.features && device.features.restore === false ? "" : `<button class="btn primary" data-action="restore">${icon("restore")}Restore last backup</button>`}
      <button class="btn" data-action="accept">${icon("check")}Keep current</button></div></div></div>`);
  }
  for (const problem of cfg.boot_problems || []) {
    out.push(`<div class="banner err">${icon("warn")}<div class="grow"><strong>Boot problem:</strong> ${esc(problem)}. See <em>Recovering an ExtremeXOS switch by hand</em> in the backups documentation.</div></div>`);
  }
  if (cfg.unsaved) {
    out.push(`<div class="banner warn">${icon("save")}<div class="grow"><strong>Unsaved changes.</strong> They are lost if the switch loses power. <div class="banner-actions"><button class="btn primary" data-action="save">${icon("save")}Save now</button></div></div></div>`);
  }
  for (const problem of (device.problems || []).filter((p) => p.kind === "hardware")) {
    out.push(`<div class="banner err">${icon("warn")}<div class="grow"><strong>Hardware:</strong> ${esc(problem.text)}</div></div>`);
  }
  return out.join("");
}

function healthTiles(device) {
  const tiles = [];
  const add = (label, value, extra = "") => tiles.push(`<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value num">${value}</div>${extra}</div>`);
  add("Uptime", esc(fmtUptime(device.uptime)));
  if (device.cpu != null) add("CPU", fmtPct(device.cpu), `<div class="meter"><i class="${meterClass(device.cpu)}" style="width:${Math.min(100, device.cpu)}%"></i></div>`);
  if (device.memory != null) add("Memory", fmtPct(device.memory), `<div class="meter"><i class="${meterClass(device.memory, 80, 92)}" style="width:${Math.min(100, device.memory)}%"></i></div>`);
  if (device.temperature != null) {
    const status = device.temperature_status || "";
    add("Temperature", `${esc(device.temperature)} °C`, status && status !== "Normal" ? `<span class="chip err">${esc(status)}</span>` : "");
  }
  if (device.poe && device.poe.budget_w) {
    const pct = (100 * (device.poe.used_w || 0)) / device.poe.budget_w;
    add("PoE", `${esc(device.poe.used_w ?? 0)} <span class="muted">/ ${esc(device.poe.budget_w)} W</span>`, `<div class="meter"><i class="${meterClass(pct, 75, 90)}" style="width:${Math.min(100, pct)}%"></i></div>`);
  }
  const rpm = Object.values(device.fan_rpm || {});
  if (device.fans) add("Fans", esc(device.fans), rpm.length ? `<div class="muted num" style="font-size:12px">${rpm.map((r) => `${r}`).join(" · ")} rpm</div>` : "");
  const psus = Object.entries(device.power_supplies || {});
  if (psus.length) add("Power supplies", psus.map(([n, s]) => `<div style="font-size:13px"><span class="muted">${esc(n)}</span> ${esc(s)}</div>`).join(""));
  return `<div class="stats">${tiles.join("")}</div>`;
}

function infoList(device) {
  const cfg = device.config || {};
  const rows = [
    ["Model", device.model],
    ["Firmware", device.firmware],
    ["IP address", device.ip || device.host],
    ["MAC address", device.mac],
    ["Serial number", device.serial],
    ["Booted image", cfg.image_booted],
    ["Booted config", cfg.config_booted],
    device.features && device.features.backup === false ? null : ["Last backup", cfg.last_backup ? `${fmtAgo(cfg.last_backup)} (${cfg.backups} kept)` : "None yet"],
  ].filter((row) => row && row[1] != null && row[1] !== "");
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

/* ------------------------------------------------------------------ */
/* Panel                                                                */
/* ------------------------------------------------------------------ */

const PANEL_CSS = `
:host { display: block; height: 100%; overflow-y: auto; background: var(--ah-bg); }
.toolbar {
  position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 4px;
  height: var(--header-height, 56px); padding: 0 12px 0 4px;
  background: var(--app-header-background-color, var(--ah-card)); color: var(--app-header-text-color, var(--ah-text));
  border-bottom: var(--app-header-border-bottom, 1px solid var(--ah-line));
}
.toolbar .title { font-size: 20px; font-weight: 400; margin-left: 8px; }
.tabs { display: flex; gap: 2px; min-width: 0; overflow-x: auto; scrollbar-width: none; }
.refresh-all { margin-left: auto; color: inherit; }
.tab {
  height: 36px; padding: 0 14px; border: 0; border-radius: 18px; background: transparent; cursor: pointer; color: inherit; opacity: 0.75; font-weight: 500;
  white-space: nowrap; flex: none;
}
.tab[aria-selected="true"] { background: color-mix(in srgb, var(--ah-accent) 16%, transparent); color: var(--ah-accent); opacity: 1; }
.content { max-width: 1240px; margin: 0 auto; padding: 20px 16px 48px; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.summary .card { padding: 14px 16px; }
.summary .big { font-size: 26px; font-weight: 500; line-height: 1.2; }
.summary .label { font-size: 13px; color: var(--ah-muted); display: flex; align-items: center; gap: 6px; }
.summary .bad .big { color: var(--ah-err); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.tile { display: flex; flex-direction: column; gap: 10px; padding: 16px; text-align: left; cursor: pointer; width: 100%; }
.tile:hover { border-color: color-mix(in srgb, var(--ah-accent) 50%, var(--ah-line)); }
.tile-head { display: flex; align-items: center; gap: 12px; }
.avatar {
  display: grid; place-items: center; width: 40px; height: 40px; border-radius: 12px; flex: none;
  background: color-mix(in srgb, var(--ah-accent) 14%, transparent); color: var(--ah-accent);
}
.avatar .icon { width: 22px; height: 22px; }
.avatar.off { background: color-mix(in srgb, var(--ah-text) 10%, transparent); color: var(--ah-muted); }
.tile-name { font-size: 16px; font-weight: 500; }
.tile-sub { font-size: 13px; color: var(--ah-muted); }
.tile-stats { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--ah-muted); }
.tile-stats b { color: var(--ah-text); font-weight: 500; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }

.detail-head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; margin-bottom: 16px; }
.detail-head h1 { margin: 0; font-size: 26px; font-weight: 400; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.banner { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; border-radius: 12px; margin-bottom: 12px; }
.banner > .icon { margin-top: 2px; }
.banner.err { background: color-mix(in srgb, var(--ah-err) 11%, var(--ah-card)); border: 1px solid color-mix(in srgb, var(--ah-err) 35%, transparent); }
.banner.err > .icon { color: var(--ah-err); }
.banner.warn { background: color-mix(in srgb, var(--ah-warn) 12%, var(--ah-card)); border: 1px solid color-mix(in srgb, var(--ah-warn) 40%, transparent); }
.banner.warn > .icon { color: var(--ah-warn); }
.banner-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
.panel-card { padding: 16px; margin-bottom: 16px; }
.fp-scroll { overflow-x: auto; padding: 8px 6px 4px; margin: -8px -6px 0; }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 14px 18px; }
.stat-label { font-size: 12px; color: var(--ah-muted); }
.stat-value { font-size: 18px; font-weight: 500; margin-bottom: 4px; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
.filter { height: 30px; padding: 0 12px; border-radius: 15px; border: 1px solid var(--ah-line); background: transparent; cursor: pointer; font-size: 13px; color: var(--ah-muted); }
.filter[aria-pressed="true"] { background: color-mix(in srgb, var(--ah-accent) 14%, transparent); border-color: transparent; color: var(--ah-accent); font-weight: 500; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-weight: 500; color: var(--ah-muted); font-size: 12px; padding: 8px 10px; border-bottom: 1px solid var(--ah-line); white-space: nowrap; }
td { padding: 9px 10px; border-bottom: 1px solid var(--ah-line); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
tbody tr.click { cursor: pointer; }
tbody tr.click:hover { background: color-mix(in srgb, var(--ah-accent) 6%, transparent); }
.table-wrap { overflow-x: auto; }
.radios { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.radio { padding: 14px; border: 1px solid var(--ah-line); border-radius: 10px; }
.search { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 12px; border-radius: 20px; border: 1px solid var(--ah-line); background: var(--ah-card); margin-bottom: 14px; max-width: 420px; }
.search input { border: 0; outline: 0; background: transparent; font: inherit; color: var(--ah-text); flex: 1; min-width: 0; }
.search:focus-within { border-color: var(--ah-accent); }
.signal { display: inline-flex; gap: 2px; align-items: flex-end; height: 12px; margin-right: 6px; }
.signal i { width: 3px; background: color-mix(in srgb, var(--ah-text) 18%, transparent); border-radius: 1px; }
.signal i.on { background: var(--ah-ok); }
.setup-intro { margin: -6px 0 16px; font-size: 13px; max-width: 820px; }
.setup-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 460px), 1fr)); gap: 16px; align-items: start; }
.setup-grid .panel-card { margin-bottom: 0; }
.setup-grid .wide { grid-column: 1 / -1; }
.card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.card-head h2 { margin: 0; font-size: 16px; font-weight: 500; }
.matrix th, .matrix td { padding: 6px 8px; }
.matrix td.mid { text-align: center; }
.matrix input { width: 18px; height: 18px; accent-color: var(--ah-accent); cursor: pointer; }
.matrix th.rot { vertical-align: bottom; text-align: center; max-width: 64px; white-space: normal; line-height: 1.2; font-size: 11.5px; }
.matrix tbody th { font-weight: 500; color: var(--ah-text); white-space: nowrap; border-bottom: 1px solid var(--ah-line); text-align: left; }
.plan-errors { margin: 4px 0 0; padding-left: 18px; }
.plan-select { width: 100%; height: 38px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--ah-line); background: var(--ah-bg); color: var(--ah-text); font: inherit; }
.wide-dialog { width: min(620px, 100%); }
.two, .three { display: grid; gap: 0 12px; }
.two { grid-template-columns: 1fr 1fr; }
.three { grid-template-columns: 1fr 1fr 1fr; }
.btn.small { height: 30px; padding: 0 10px; font-size: 13px; }
.row-actions { display: inline-flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.key-box { font-size: 22px; letter-spacing: 1px; padding: 14px; border-radius: 10px; background: color-mix(in srgb, var(--ah-text) 6%, transparent); text-align: center; user-select: all; overflow-wrap: anywhere; }
.apply-row { padding: 10px 0; border-top: 1px solid var(--ah-line); }
.apply-row:first-of-type { border-top: 0; }
.apply-row details { margin-top: 6px; }
.apply-row summary { cursor: pointer; font-size: 13px; color: var(--ah-muted); }
.cmds { max-height: 260px; overflow: auto; font-size: 11.5px; background: color-mix(in srgb, var(--ah-text) 5%, transparent); padding: 8px; border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.chip.ok { color: var(--ah-ok); }
.chip.bad { color: var(--ah-err); }
.activity { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; font-size: 13px; }
.qr-box { display: flex; justify-content: center; margin: 14px 0 4px; }
.gw-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; font-size: 13.5px; }
.gw-list li { display: flex; gap: 10px; align-items: flex-start; }
.gw-list .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; flex: none; background: var(--ah-muted); }
.gw-list .dot.ok { background: var(--ah-ok); }
.gw-list .dot.bad { background: var(--ah-err); }
.portal-dialog { width: min(1040px, 100%); }
.rules { display: grid; gap: 10px; margin-bottom: 12px; }
.rule-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) auto auto; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--ah-line); border-radius: 10px; }
.rule-row .toggle { font-size: 13px; }
.portal-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 380px); gap: 20px; align-items: start; }
.portal-settings h3 { margin: 18px 0 0; font-size: 14px; font-weight: 600; }
.portal-settings h3:first-child { margin-top: 0; }
.portal-preview-col { position: sticky; top: 0; }
.portal-preview { width: 100%; height: 560px; border: 1px solid var(--ah-line); border-radius: 12px; background: #fff; }
.dialog textarea { width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--ah-line); background: var(--ah-bg); color: var(--ah-text); font: inherit; resize: vertical; }
.dialog textarea#pt-html { font-family: var(--code-font-family, ui-monospace, Menlo, monospace); font-size: 12px; }
.colors { display: flex; flex-wrap: wrap; gap: 10px 18px; }
.color-pick { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; }
.color-pick input { width: 36px; height: 28px; padding: 0; border: 1px solid var(--ah-line); border-radius: 6px; background: none; }
.logo-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.logo-thumb { max-height: 40px; max-width: 120px; border-radius: 6px; background: #fff; padding: 2px; }
.file-btn { position: relative; overflow: hidden; }
.file-btn input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.qr-box svg { width: min(220px, 70vw); height: auto; border-radius: 8px; }
.err-text { color: var(--ah-err); }
@media (max-width: 900px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 600px) {
  .content { padding: 16px 16px 40px; }
  .toolbar .title { font-size: 18px; }
  .tab { padding: 0 8px; }
  .toolbar .title { display: none; }
  .detail-head h1 { font-size: 22px; }
  .grid { grid-template-columns: minmax(0, 1fr); }
  .hide-sm { display: none; }
  .port { width: 26px; height: 22px; }
  .two, .three { grid-template-columns: minmax(0, 1fr); }
  .portal-grid { grid-template-columns: minmax(0, 1fr); }
  .rule-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .portal-preview { height: 460px; }
}
`;

function signalBars(dbm) {
  if (dbm == null) return "";
  const level = dbm >= -55 ? 4 : dbm >= -65 ? 3 : dbm >= -75 ? 2 : 1;
  return `<span class="signal" aria-hidden="true">${[4, 7, 10, 12].map((h, i) => `<i class="${i < level ? "on" : ""}" style="height:${h}px"></i>`).join("")}</span>`;
}

class AerohiveNetworkPanel extends AerohiveBase {
  constructor() {
    super();
    this._path = "";
    this._tab = "devices";
    this._portFilter = "all";
    this._search = "";
    this._popstate = () => this._syncFromLocation();
    rowKeyboard(this.shadowRoot);
    this.shadowRoot.innerHTML = `<style>${BASE_CSS}${PANEL_CSS}</style>
      <div class="toolbar"><span class="menu"></span><span class="title">Network</span>
        <button class="icon-btn refresh-all" data-action="refresh" aria-label="Refresh all devices now" title="Refresh all devices now">${icon("reboot")}</button>
        <div class="tabs" role="tablist">
          <button class="tab" role="tab" data-action="tab" data-tab="devices">Devices</button>
          <button class="tab" role="tab" data-action="tab" data-tab="wifi">Wi-Fi</button>
          <button class="tab" role="tab" data-action="tab" data-tab="clients">Clients</button>
          <button class="tab" role="tab" data-action="tab" data-tab="setup">Setup</button>
        </div></div>
      <div class="content" id="content"></div><div id="dialog"></div>`;
    this.shadowRoot.addEventListener("input", (ev) => {
      if (ev.target.id === "client-search") {
        this._search = ev.target.value;
        this._renderContent();
      }
    });
  }

  set narrow(value) {
    this._narrow = value;
    this._renderMenu();
  }

  set panel(value) {
    this._panel = value;
  }

  set route(route) {
    this._route = route;
    if (route && typeof route.path === "string") this._setPath(route.path);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener("popstate", this._popstate);
    if (!this._route) this._syncFromLocation();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener("popstate", this._popstate);
  }

  _syncFromLocation() {
    const path = window.location.pathname;
    const index = path.indexOf(`/${PANEL_PATH}`);
    if (index >= 0) this._setPath(path.slice(index + PANEL_PATH.length + 1));
  }

  _setPath(path) {
    const clean = (path || "").replace(/^\/+|\/+$/g, "");
    if (clean === this._path) return;
    this._path = clean;
    this._tab = ["clients", "wifi", "setup"].includes(clean) ? clean : "devices";
    this._render();
  }

  _navigate(path) {
    const url = `/${PANEL_PATH}${path ? `/${path}` : ""}`;
    if (window.location.pathname.includes(`/${PANEL_PATH}`)) {
      window.history.pushState(null, "", url);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
    }
    this._setPath(path);
    const content = this.shadowRoot.getElementById("content");
    if (content) content.scrollIntoView({ block: "start" });
  }

  _onHass() {
    this._renderMenu();
  }

  _renderMenu() {
    const holder = this.shadowRoot.querySelector(".menu");
    if (!holder || !this._hass) return;
    if (customElements.get("ha-menu-button")) {
      let button = holder.querySelector("ha-menu-button");
      if (!button) {
        button = document.createElement("ha-menu-button");
        holder.appendChild(button);
      }
      button.hass = this._hass;
      button.narrow = this._narrow;
    }
  }

  _defaultDevice() {
    return this._path && !["clients", "wifi", "setup"].includes(this._path) ? this._path : null;
  }

  _render() {
    if (!this.shadowRoot.getElementById("content")) return;
    for (const tab of this.shadowRoot.querySelectorAll(".tab")) {
      tab.setAttribute("aria-selected", String(tab.dataset.tab === this._tab));
    }
    const refreshAll = this.shadowRoot.querySelector(".refresh-all");
    if (refreshAll) {
      refreshAll.disabled = this._isBusy("refresh:all");
      refreshAll.querySelector(".icon").classList.toggle("spin", this._isBusy("refresh:all"));
    }
    this._renderContent();
    this._renderDialogInto(this.shadowRoot.getElementById("dialog"));
  }

  _renderContent() {
    const content = this.shadowRoot.getElementById("content");
    if (!content) return;
    if (content.contains(this.shadowRoot.activeElement) && this.shadowRoot.activeElement.id === "client-search") {
      // Only the results change while typing.
      const results = content.querySelector("#client-results");
      if (results) results.innerHTML = this._clientRows();
      return;
    }
    if (this._tab === "setup") {
      const active = this.shadowRoot.activeElement;
      // Don't redraw under a field being typed in (trunk ports, router).
      if (active && content.contains(active) && active.matches("input[type=text], select")) return;
      content.innerHTML = this._setupView();
      return;
    }
    if (this._store && this._store.error) {
      content.innerHTML = `<div class="card empty"><p><strong>Can't load the network data.</strong></p><p>${esc(this._store.error)}</p><p>Restart Home Assistant after updating the integration.</p></div>`;
      return;
    }
    if (!this._data) {
      content.innerHTML = `<div class="empty">Loading…</div>`;
      return;
    }
    if (!this._data.devices.length) {
      content.innerHTML = `<div class="card empty"><p><strong>No devices yet.</strong></p><p>Add an access point or switch under Settings → Devices &amp; services → Add integration → Aerohive / Extreme Networks (Local SSH).</p></div>`;
      return;
    }
    if (this._tab === "clients") content.innerHTML = this._clientsView();
    else if (this._tab === "wifi") content.innerHTML = this._wifiView();
    else if (this._path) {
      const device = findDevice(this._data, this._path);
      content.innerHTML = device ? this._deviceView(device) : `<div class="card empty">This device isn't loaded. <button class="btn" data-action="home">Back to all devices</button></div>`;
    } else content.innerHTML = this._overview();
  }

  /* ---------------- overview ---------------- */

  _overview() {
    const devices = this._data.devices;
    const online = devices.filter((d) => d.online).length;
    const wireless = devices.reduce((n, d) => n + (d.stations || []).length, 0);
    const wired = devices.reduce((n, d) => n + (d.wired_clients || 0), 0);
    const poeUsed = devices.reduce((n, d) => n + ((d.poe && d.poe.used_w) || 0), 0);
    const poeBudget = devices.reduce((n, d) => n + ((d.poe && d.poe.budget_w) || 0), 0);
    const problems = devices.reduce((n, d) => n + (d.online ? (d.problems || []).length : 1), 0);
    return `
      <div class="summary">
        <div class="card"><div class="label">${icon("switch")}Devices online</div><div class="big num">${online}<span class="muted" style="font-size:16px"> / ${devices.length}</span></div></div>
        <div class="card"><div class="label">${icon("wifi")}Wi-Fi clients</div><div class="big num">${wireless}</div></div>
        <div class="card"><div class="label">${icon("clients")}Wired clients</div><div class="big num">${wired}</div></div>
        ${poeBudget ? `<div class="card"><div class="label">${icon("bolt")}PoE power</div><div class="big num">${Math.round(poeUsed * 10) / 10}<span class="muted" style="font-size:16px"> / ${poeBudget} W</span></div></div>` : ""}
        <div class="card ${problems ? "bad" : ""}"><div class="label">${icon("warn")}Problems</div><div class="big num">${problems}</div></div>
      </div>
      <div class="grid">${devices.map((d) => this._tile(d)).join("")}</div>`;
  }

  _tile(device) {
    const stats = device.online
      ? [
          `Up <b class="num">${esc(fmtUptime(device.uptime))}</b>`,
          device.cpu != null ? `CPU <b class="num">${fmtPct(device.cpu)}</b>` : "",
          device.memory != null ? `Memory <b class="num">${fmtPct(device.memory)}</b>` : "",
          device.temperature != null ? `<b class="num">${esc(device.temperature)} °C</b>` : "",
        ].filter(Boolean)
      : [];
    return `
      <button class="card tile" data-action="open" data-open="${esc(device.entry_id)}" aria-label="${esc(`${device.name}: ${deviceSummary(device)}`)}">
        <div class="tile-head">
          <span class="avatar ${device.online ? "" : "off"}">${deviceIcon(device)}</span>
          <div class="grow">
            <div class="tile-name ellipsis">${esc(device.name)}</div>
            <div class="tile-sub ellipsis">${esc([device.model, device.ip || device.host].filter(Boolean).join(" · "))}</div>
          </div>
          <span class="dot ${device.online ? ((device.problems || []).length ? "warn" : "ok") : "err"}"></span>
        </div>
        ${device.kind === "switch" ? renderMiniFaceplate(device) : ""}
        <div class="tile-sub">${esc(deviceSummary(device))}</div>
        ${stats.length ? `<div class="tile-stats">${stats.join("")}</div>` : ""}
        ${(device.problems || []).length || !device.online ? `<div class="chips">${problemChips(device)}</div>` : ""}
      </button>`;
  }

  /* ---------------- device ---------------- */

  _deviceView(device) {
    const isSwitch = device.ports && device.ports.length;
    return `
      <div data-device="${esc(device.entry_id)}">
        <div class="detail-head">
          <button class="icon-btn" data-action="home" aria-label="All devices">${icon("back")}</button>
          <span class="avatar ${device.online ? "" : "off"}">${deviceIcon(device)}</span>
          <div class="grow" style="min-width:180px">
            <h1 class="ellipsis">${esc(device.name)}</h1>
            <div class="muted">${esc([device.model, device.firmware].filter(Boolean).join(" · "))}</div>
          </div>
          ${deviceActions(this, device)}
        </div>
        ${deviceBanners(device)}
        ${isSwitch ? this._faceplateCard(device) : ""}
        <div class="layout">
          <div>
            ${device.kind === "unknown" ? this._unknownCard(device) : ""}
            ${isSwitch ? this._portsCard(device) : ""}
            ${device.radios && device.radios.length ? this._radiosCard(device) : ""}
            ${device.ssids && device.ssids.length ? this._ssidsCard(device) : ""}
            ${device.stations && device.stations.length ? this._stationsCard(device) : ""}
            ${device.vlans && device.vlans.length ? this._vlansCard(device) : ""}
            ${device.networks && device.networks.length ? this._networksCard(device) : ""}
          </div>
          <div>
            ${device.gateways && device.gateways.length ? this._gatewaysCard(device) : ""}
            <div class="card panel-card"><div class="section-title">Health</div>${healthTiles(device)}</div>
            <div class="card panel-card"><div class="section-title">Device</div>${infoList(device)}</div>
          </div>
        </div>
      </div>`;
  }

  _gatewaysCard(device) {
    return `<div class="card panel-card"><div class="section-title">Internet gateways</div>
      <ul class="gw-list">${device.gateways
        .map((g) => `<li><span class="dot ${g.online === false ? "bad" : g.online ? "ok" : ""}"></span><div class="grow"><strong>${esc(g.name)}</strong>${g.address ? ` <span class="muted mono">${esc(g.address)}</span>` : ""}<br>
          <span class="muted">${g.online === false ? esc(g.status || "down") : g.online ? "Online" : esc(g.status || "Unknown")}${g.delay ? ` · ${esc(g.delay)}` : ""}${g.loss ? ` · loss ${esc(g.loss)}` : ""}</span></div></li>`)
        .join("")}</ul></div>`;
  }

  _networksCard(device) {
    return `<div class="card panel-card"><div class="section-title">Networks</div><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Interface</th><th>Address</th><th class="num">VLAN</th></tr></thead>
      <tbody>${device.networks.map((n) => `<tr><td>${esc(n.name)}</td><td class="mono">${esc(n.device)}</td><td class="mono">${esc(n.address)}</td><td class="num">${n.vlan != null ? esc(n.vlan) : ""}</td></tr>`).join("")}</tbody>
    </table></div></div>`;
  }

  _unknownCard(device) {
    return `<div class="card panel-card">
      <div class="section-title">Device type not recognised</div>
      <p style="margin:0 0 10px">Home Assistant logged in to ${esc(device.name)} at ${esc(device.host)}, but its replies didn't match an
        access point, SR switch or ExtremeXOS switch, so there are no ports or radios to show.</p>
      <p class="muted" style="margin:0">Press <strong>Refresh</strong> to try again. If it stays like this, please
        <a href="https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose" target="_blank" rel="noreferrer">request support for the device</a>
        and attach its diagnostics (Settings → Devices &amp; services → Aerohive → ⋮ → Download diagnostics) so it can be added.</p>
    </div>`;
  }

  _portFilterFn() {
    switch (this._portFilter) {
      case "up":
        return (p) => p.up;
      case "poe":
        return (p) => p.poe && p.poe.delivering;
      case "issues":
        return (p) => p.enabled === false || p.errors;
      default:
        return null;
    }
  }

  _faceplateCard(device) {
    const selected = this._dialog && this._dialog.type === "port" ? this._dialog.port : null;
    const ports = device.ports || [];
    const up = ports.filter((p) => p.up).length;
    return `
      <div class="card panel-card">
        <div class="row" style="margin-bottom:10px"><div class="section-title grow" style="margin:0">Front panel</div>
          <span class="muted num" style="font-size:13px">${up} of ${ports.length} connected · select a port to manage it</span></div>
        <div class="fp-scroll">${renderFaceplate(device, { selected, filter: this._portFilterFn() })}</div>
        ${renderLegend(device)}
      </div>`;
  }

  _portsCard(device) {
    const filter = this._portFilterFn();
    const ports = [...device.ports].sort((a, b) => natCompare(a.name, b.name));
    const shown = filter ? ports.filter(filter) : ports.filter((p) => p.present !== false);
    const names = clientNames(this._data);
    const filters = [
      ["all", "All"],
      ["up", `Connected (${ports.filter((p) => p.up).length})`],
      ...(device.features && device.features.poe ? [["poe", `PoE (${ports.filter((p) => p.poe && p.poe.delivering).length})`]] : []),
      ["issues", "Disabled / errors"],
    ];
    return `
      <div class="card panel-card">
        <div class="section-title">Ports</div>
        <div class="filters" role="group" aria-label="Filter ports">
          ${filters.map(([key, label]) => `<button class="filter" data-action="port-filter" data-filter="${key}" aria-pressed="${this._portFilter === key}">${esc(label)}</button>`).join("")}
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Port</th><th>Status</th><th class="hide-sm">Connected to</th>${device.features && device.features.poe ? "<th>PoE</th>" : ""}<th class="hide-sm">Traffic</th></tr></thead>
          <tbody>${
            shown.length
              ? shown
                  .map((p) => {
                    const neighbor = p.neighbor && p.neighbor.name ? p.neighbor.name : p.clients && p.clients.length === 1 ? names.get(p.clients[0].mac) || p.clients[0].mac : p.clients && p.clients.length ? `${p.clients.length} devices` : "";
                    return `<tr class="click" data-port="${esc(p.name)}" tabindex="0">
                      <td><strong>${esc(p.name)}</strong>${p.label ? ` <span class="muted">${esc(p.label)}</span>` : ""}${p.uplink ? ` <span class="chip">uplink</span>` : ""}</td>
                      <td><span class="row"><span class="dot ${p.up ? "ok" : ""}" style="${p.up ? "" : "opacity:.5"}"></span>${esc(portStatusText(p))}</span></td>
                      <td class="hide-sm ellipsis" style="max-width:220px">${esc(neighbor)}</td>
                      ${device.features && device.features.poe ? `<td class="num">${p.poe ? (p.poe.delivering ? `${esc(p.poe.power_w ?? 0)} W` : `<span class="muted">${p.poe.enabled === false ? "Off" : "–"}</span>`) : ""}</td>` : ""}
                      <td class="hide-sm num muted">${p.up && p.rx_kbps != null ? `↓ ${fmtRate(p.rx_kbps)} ↑ ${fmtRate(p.tx_kbps)}` : ""}</td>
                    </tr>`;
                  })
                  .join("")
              : `<tr><td colspan="5" class="muted">No ports match.</td></tr>`
          }</tbody></table></div>
      </div>`;
  }

  _radiosCard(device) {
    return `<div class="card panel-card"><div class="section-title">Radios</div><div class="radios">${device.radios
      .map(
        (r) => `<div class="radio">
          <div class="row" style="margin-bottom:8px"><span class="dot ${r.up ? "ok" : "err"}"></span><strong class="grow">${esc(r.band || r.name)}</strong><span class="muted">${esc(r.name)}</span></div>
          <dl class="kv">
            <dt>Channel</dt><dd class="num">${esc(r.channel ?? "–")}${r.width ? ` <span class="muted">${esc(r.width)} MHz</span>` : ""}</dd>
            <dt>Power</dt><dd class="num">${r.tx_power != null ? `${esc(r.tx_power)} dBm` : "–"}</dd>
            <dt>Clients</dt><dd class="num">${esc(r.clients)}</dd>
            ${r.utilization != null ? `<dt>Busy</dt><dd><div class="row"><div class="meter grow"><i class="${meterClass(r.utilization, 50, 75)}" style="width:${Math.min(100, r.utilization)}%"></i></div><span class="num">${fmtPct(r.utilization)}</span></div></dd>` : ""}
            ${r.noise != null ? `<dt>Noise</dt><dd class="num">${esc(r.noise)} dBm</dd>` : ""}
            <dt>SSIDs</dt><dd>${r.ssids.length ? r.ssids.map((s) => `<span class="chip">${esc(s)}</span>`).join(" ") : '<span class="muted">none</span>'}</dd>
          </dl></div>`,
      )
      .join("")}</div></div>`;
  }

  _ssidsCard(device) {
    return `<div class="card panel-card"><div class="section-title">Wi-Fi networks</div><div class="table-wrap"><table>
      <thead><tr><th>SSID</th><th>Status</th><th>Clients</th></tr></thead>
      <tbody>${device.ssids
        .map(
          (s) => `<tr ${s.configurable === false ? 'title="Listed by the access point, not one of its configured networks"' : `class="click" data-action="wifi-edit" data-ssid="${esc(s.name)}" data-scope="${esc(device.entry_id)}" tabindex="0"`}><td><strong>${esc(s.name)}</strong>${s.hidden ? ' <span class="chip">hidden</span>' : ""}${s.configurable === false ? ' <span class="chip">read-only</span>' : ""}</td>
            <td><span class="row"><span class="dot ${s.broadcasting ? "ok" : ""}"></span>${s.broadcasting ? "Broadcasting" : "Off"}</span></td>
            <td class="num">${esc(s.clients)}</td></tr>`,
        )
        .join("")}</tbody></table></div>
      <div class="row" style="margin-top:12px"><span class="muted grow" style="font-size:12px">Select a network to change it on this access point.</span>
        <button class="btn" data-action="tab" data-tab="wifi">${icon("wifi")}All Wi-Fi networks</button></div></div>`;
  }

  _stationsCard(device) {
    const rows = [...device.stations].sort((a, b) => (b.signal ?? -999) - (a.signal ?? -999));
    return `<div class="card panel-card"><div class="section-title">Wi-Fi clients (${rows.length})</div><div class="table-wrap"><table>
      <thead><tr><th>Client</th><th>SSID</th><th>Signal</th><th class="hide-sm">Rate</th></tr></thead>
      <tbody>${rows
        .map(
          (s) => `<tr><td>${s.hostname ? `${esc(s.hostname)}<br>` : ""}<span class="mono muted">${esc(s.mac)}</span>${s.ip ? ` <span class="muted">${esc(s.ip)}</span>` : ""}</td>
            <td>${esc(s.ssid || "")}</td><td class="num">${signalBars(s.signal)}${s.signal != null ? `${esc(s.signal)} dBm` : "–"}</td>
            <td class="hide-sm num muted">${esc(s.tx_rate || "")}</td></tr>`,
        )
        .join("")}</tbody></table></div></div>`;
  }

  _vlansCard(device) {
    return `<div class="card panel-card"><div class="section-title">VLANs</div><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>ID</th><th>IP address</th><th>Ports</th></tr></thead>
      <tbody>${device.vlans
        .map((v) => `<tr><td><strong>${esc(v.name)}</strong></td><td class="num">${esc(v.vid)}</td><td class="mono">${v.ip ? `${esc(v.ip)}/${esc(v.prefix)}` : '<span class="muted">–</span>'}</td><td class="num">${esc(v.active ?? "–")} <span class="muted">/ ${esc(v.total ?? "–")} active</span></td></tr>`)
        .join("")}</tbody></table></div></div>`;
  }

  /* ---------------- Wi-Fi ---------------- */

  _wifiView() {
    const { aps, networks } = wifiNetworks(this._data);
    if (!aps.length) {
      return `<div class="card empty"><p><strong>No access points online.</strong></p><p>Wi-Fi networks are managed on the access points; add one, or check that it's reachable.</p></div>`;
    }
    const admin = this._isAdmin();
    const clients = networks.reduce((n, net) => n + net.clients, 0);
    return `
      <div class="detail-head" style="margin-bottom:16px">
        <div class="grow"><h1 style="font-size:22px">Wi-Fi networks</h1>
          <div class="muted">${networks.length} network${networks.length === 1 ? "" : "s"} on ${aps.length} access point${aps.length === 1 ? "" : "s"} · ${clients} client${clients === 1 ? "" : "s"}</div></div>
        ${admin ? `<button class="btn primary" data-action="wifi-add">${icon("plus")}Add network</button>` : ""}
      </div>
      ${admin ? "" : '<div class="banner warn">' + icon("lock") + '<div class="grow">Only administrators can change Wi-Fi networks.</div></div>'}
      ${networks.length ? `<div class="grid">${networks.map((n) => this._networkCard(n)).join("")}</div>` : '<div class="card empty">No Wi-Fi networks yet.</div>'}
      <p class="muted" style="font-size:12px;margin-top:16px">Changes apply to every access point with the network unless you pick some in Edit,
        and are saved on them within a couple of seconds. Timed changes go back on their own, also after a restart.</p>`;
  }

  /* ---------------- clients ---------------- */

  _allClients() {
    const devices = this._data.devices;
    const wirelessMacs = new Set();
    const names = clientNames(this._data);
    const rows = [];
    for (const d of devices) {
      for (const s of d.stations || []) {
        wirelessMacs.add(s.mac);
        const info = (this._data.clients || {})[s.mac] || {};
        rows.push({ mac: s.mac, name: info.name || s.hostname, vendor: info.vendor, private: info.private, type: info.kind, ip: info.ip || s.ip, where: `${d.name} · ${s.ssid || s.radio || "Wi-Fi"}`, signal: s.signal, kind: "wifi", device: d.entry_id });
      }
    }
    // A device can be learned on several switches (on the way to it): list it once, at the
    // port with the fewest devices (its edge port).
    const edge = new Map();
    for (const d of devices) {
      for (const p of d.ports || []) {
        if (p.uplink) continue;
        for (const c of p.clients || []) {
          const best = edge.get(c.mac);
          if (!best || (p.clients || []).length < best) edge.set(c.mac, (p.clients || []).length);
        }
      }
    }
    const listed = new Set();
    for (const d of devices) {
      for (const p of d.ports || []) {
        if (p.uplink) continue;
        for (const c of p.clients || []) {
          if (wirelessMacs.has(c.mac) || listed.has(c.mac) || (p.clients || []).length !== edge.get(c.mac)) continue;
          listed.add(c.mac);
          const info = (this._data.clients || {})[c.mac] || {};
          rows.push({ mac: c.mac, name: names.get(c.mac) || (p.neighbor && p.clients.length === 1 ? p.neighbor.name : null), vendor: info.vendor, private: info.private, type: info.kind, ip: info.ip, where: `${d.name} · port ${p.name}${p.label ? ` (${p.label})` : ""}`, vlan: c.vlan, kind: "wired", device: d.entry_id, port: p.name });
        }
      }
    }
    for (const d of devices) {
      for (const mac of d.lan_clients || []) {
        if (wirelessMacs.has(mac) || listed.has(mac)) continue;
        listed.add(mac);
        const info = (this._data.clients || {})[mac] || {};
        rows.push({ mac, name: names.get(mac), vendor: info.vendor, private: info.private, type: info.kind, ip: info.ip, where: `${d.name} · LAN`, kind: "wired", device: d.entry_id });
      }
    }
    return rows;
  }

  _clientRows() {
    const query = this._search.trim().toLowerCase();
    const rows = this._allClients().filter((r) => !query || [r.mac, r.name, r.ip, r.where, r.vendor, KIND_LABELS[r.type]].some((v) => v && String(v).toLowerCase().includes(query)));
    if (!rows.length) return `<tr><td colspan="4" class="muted">${query ? "No clients match." : "No clients."}</td></tr>`;
    return rows
      .sort((a, b) => (a.name || "~" + a.mac).localeCompare(b.name || "~" + b.mac))
      .map(
        (r) => `<tr class="click" data-client-mac="${esc(r.mac)}" tabindex="0">
          <td>${r.name ? `<strong>${esc(r.name)}</strong><br>` : ""}<span class="muted">${esc([r.type ? KIND_LABELS[r.type] : null, r.vendor || (r.private ? "Private address" : null)].filter(Boolean).join(" · "))}</span>${r.type || r.vendor || r.private ? "<br>" : ""}<span class="mono muted">${esc(r.mac)}</span></td>
          <td><span class="chip">${r.kind === "wifi" ? `${icon("wifi")}Wi-Fi` : `${icon("switch")}Wired`}</span></td>
          <td>${esc(r.where)}</td>
          <td class="num hide-sm">${r.kind === "wifi" ? `${signalBars(r.signal)}${r.signal != null ? `${esc(r.signal)} dBm` : ""}` : r.vlan != null ? `VLAN ${esc(r.vlan)}` : ""}${r.ip ? `<br><span class="muted">${esc(r.ip)}</span>` : ""}</td>
        </tr>`,
      )
      .join("");
  }

  _clientsView() {
    const total = this._allClients().length;
    return `
      <label class="search">${icon("search")}<input id="client-search" type="search" placeholder="Search ${total} clients by name, maker, MAC, IP or port" value="${esc(this._search)}" aria-label="Search clients"></label>
      <div class="card"><div class="table-wrap"><table>
        <thead><tr><th>Client</th><th>Type</th><th>Connected to</th><th class="hide-sm">Details</th></tr></thead>
        <tbody id="client-results">${this._clientRows()}</tbody>
      </table></div></div>`;
  }

  /* ---------------- setup tab hooks ---------------- */

  _dialogHtml() {
    if (this._dialog && this._dialog.type.startsWith("plan-")) return this._planDialog(this._dialog);
    return super._dialogHtml();
  }

  _handleAction(action, el) {
    if (action.startsWith("plan-")) {
      this._planAction(action, el);
      return;
    }
    super._handleAction(action, el);
  }

  _onChange(ev) {
    const target = ev.target;
    if (target.dataset && target.dataset.plan && !target.closest(".dialog")) {
      this._planInput(target);
      return;
    }
    if (target.dataset && target.dataset.rule !== undefined && this._dialog && this._dialog.type === "plan-rules") {
      const rule = this._dialog.rules[Number(target.dataset.rule)];
      const field = target.dataset.ruleField;
      if (field === "auto") rule.auto = target.checked;
      else if (field === "minutes") rule.minutes = Number(target.value) || null;
      else if (field === "network") rule.network = target.value || null;
      else rule[field] = target.value;
      return;
    }
    if (target.dataset && target.dataset.logo !== undefined && this._dialog && this._dialog.type === "plan-portal") {
      const file = target.files && target.files[0];
      if (!file) return;
      if (file.size > 70 * 1024) return this._toast("The logo can be up to 70 KB", true);
      const reader = new FileReader();
      reader.onload = () => {
        this._portalForm(this._dialog).logo = String(reader.result);
        this._render();
        this._portalPreview();
      };
      reader.readAsDataURL(file);
      return;
    }
    super._onChange(ev);
    if (this._dialog && this._dialog.type === "plan-portal" && target.dataset && target.dataset.field) {
      clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => this._portalPreview(), 250);
    }
  }

  _onData(data) {
    super._onData(data);
    if (this._tab === "setup" && this._setup && !this._setup.dirty) this._setupLoad();
  }

  /* ---------------- events ---------------- */

  _onClick(ev) {
    const clientRow = ev.target.closest("[data-client-mac]");
    if (clientRow && !clientRow.closest(".dialog")) {
      this._openDialog({ type: "client", mac: clientRow.dataset.clientMac });
      return;
    }
    super._onClick(ev);
  }

  _onAction(action, el) {
    if (action === "tab") {
      this._navigate(el.dataset.tab === "devices" ? "" : el.dataset.tab);
    } else if (action === "open") {
      this._navigate(el.dataset.open);
    } else if (action === "home") {
      this._navigate("");
    } else if (action === "port-filter") {
      this._portFilter = el.dataset.filter;
      this._render();
    }
  }
}

/* ------------------------------------------------------------------ */
/* Setup tab: the network plan (networks, access, Wi-Fi, keys, devices) */
/* ------------------------------------------------------------------ */

const SECURITY_LABELS = {
  wpa2: "WPA2 password",
  wpa2_wpa3: "WPA2/WPA3 password",
  wpa3: "WPA3 password",
  ppsk: "Per-device keys",
  open: "Open (no password)",
};
const PLAN_BANDS = ["2.4 GHz", "5 GHz", "6 GHz"];
const KEY_DURATIONS = [
  [0, "Permanent"],
  [15, "15 minutes"],
  [60, "1 hour"],
  [1440, "1 day"],
  [10080, "1 week"],
];
const ROTATE_CHOICES = [
  [90, "3 months"],
  [182, "6 months"],
  [365, "year"],
];
const CUTOFF_CHOICES = [
  [0, "Until I confirm"],
  [7, "1 more week"],
  [14, "2 more weeks"],
  [30, "1 more month"],
];
const DEFAULT_KINDS = ["Phone or tablet", "Computer", "TV or streaming box", "Speaker or smart-home device", "Something else"];

function rotateLabel(days) {
  const hit = ROTATE_CHOICES.find(([d]) => d === Number(days));
  return hit ? hit[1] : `${days} days`;
}

/* Same ids as the integration makes from the labels (portal.kind_choices). */
function kindChoices(labels) {
  const list = (labels || []).map((x) => String(x).trim().slice(0, 40)).filter(Boolean);
  const out = [];
  for (const label of list.length ? list : DEFAULT_KINDS) {
    const base = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 16).replace(/_+$/, "") || "kind";
    let id = base;
    for (let n = 2; out.some(([k]) => k === id); n++) id = `${base.slice(0, 13)}_${n}`;
    out.push([id, label]);
  }
  return out.slice(0, 12);
}

function kindLabel(labels, id) {
  return (kindChoices(labels).find(([k]) => k === id) || [id, id])[1];
}

const ROLE_LABELS = { router: "Router", ap: "Access point", switch: "Switch", none: "Not used" };

function slugId(text, taken) {
  let base = String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 14) || "net";
  if (!/^[a-z]/.test(base)) base = `n${base}`.slice(0, 14);
  let id = base;
  for (let n = 2; taken.includes(id); n++) id = `${base}${n}`.slice(0, 16);
  return id;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function timeLeft(iso) {
  if (!iso) return "Permanent";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "Expiring…";
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} min left`;
  if (min < 2880) return `${Math.round(min / 60)} h left`;
  return `${Math.round(min / 1440)} days left`;
}

const SetupMixin = {
  _setupState() {
    if (!this._setup) this._setup = { state: null, draft: null, secrets: {}, dirty: false, loading: false, error: null };
    return this._setup;
  },

  async _setupWs(msg, busyKey) {
    if (busyKey) {
      if (this._busy.has(busyKey)) return null;
      this._busy.add(busyKey);
      this._render();
    }
    try {
      return await this._hass.callWS(msg);
    } catch (err) {
      this._toast((err && err.message) || String(err), true);
      return null;
    } finally {
      if (busyKey) {
        this._busy.delete(busyKey);
        this._render();
      }
    }
  },

  _setupTake(state) {
    const s = this._setupState();
    s.state = state;
    if (!s.dirty) s.draft = clone(state.plan);
    else s.draft.keys = clone(state.plan.keys); // keys are saved on their own
    s.loadedAt = Date.now();
  },

  async _setupLoad(force = false) {
    const s = this._setupState();
    const every = s.state && s.state.portal && s.state.portal.on ? 15000 : 30000; // waiting devices show up within ~15 s
    if (!this._hass || s.loading || (!force && s.loadedAt && Date.now() - s.loadedAt < every)) return;
    s.loading = true;
    try {
      this._setupTake(await this._hass.callWS({ type: `${DOMAIN}/plan/get` }));
      s.error = null;
    } catch (err) {
      s.error = (err && err.message) || String(err);
    }
    s.loading = false;
    this._render();
  },

  _setupDirty(redraw = true) {
    this._setupState().dirty = true;
    if (redraw) {
      this._render();
      return;
    }
    // A text field changed (on blur): redrawing now would swallow the click that caused the blur.
    const save = this.shadowRoot.querySelector('[data-action="plan-save"]');
    if (save) save.disabled = false;
    const status = this.shadowRoot.querySelector(".detail-head .muted");
    if (status) status.textContent = "Edits not saved yet";
  },

  _setupView() {
    if (!this._isAdmin()) {
      return `<div class="card empty"><p><strong>Only administrators can see and change the network setup.</strong></p></div>`;
    }
    const s = this._setupState();
    if (!s.state) {
      this._setupLoad();
      return s.error ? `<div class="card empty"><p><strong>Can't load the network setup.</strong></p><p>${esc(s.error)}</p></div>` : `<div class="empty">Loading…</div>`;
    }
    const st = s.state;
    const plan = s.draft;
    const status = s.dirty
      ? "Edits not saved yet"
      : st.pending
        ? "Saved · not applied to the devices yet"
        : st.applied_at
          ? `Applied ${new Date(st.applied_at).toLocaleString()}`
          : "Nothing set up yet";
    const empty = !plan.networks.length && !plan.wifi.length;
    return `
      <div class="detail-head" style="margin-bottom:16px">
        <div class="grow"><h1 style="font-size:22px">Network setup</h1><div class="muted">${esc(status)}</div></div>
        ${empty ? `<button class="btn" data-action="plan-template">${icon("plus")}Start from the home layout</button>` : ""}
        <button class="btn" data-action="plan-save" ${s.dirty && !this._isBusy("plan-save") ? "" : "disabled"}>${icon("save")}${this._isBusy("plan-save") ? "Saving…" : "Save"}</button>
        <button class="btn primary" data-action="plan-review" ${empty ? "disabled" : ""}>${icon("check")}Review &amp; apply</button>
      </div>
      ${!s.dirty && st.errors.length ? `<div class="banner err">${icon("warn")}<div class="grow"><strong>Fix before applying</strong><ul class="plan-errors">${st.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div></div>` : ""}
      ${st.needs_apply.length ? `<div class="banner warn">${icon("warn")}<div class="grow">A key change hasn't reached every device yet; it's retried every minute.</div></div>` : ""}
      <p class="muted setup-intro">Design the whole network here: networks (VLANs) with their addresses, who may reach what, Wi-Fi networks and a key per device.
        Nothing changes on the devices until you press <strong>Review &amp; apply</strong>. Passwords and keys are stored encrypted.</p>
      <div class="setup-grid">
        ${this._setupNetworksCard(plan)}
        ${this._setupAccessCard(plan)}
        ${this._setupWifiCard(plan)}
        ${this._setupKeysCard(st)}
        ${this._setupOnboardingCard(plan, st)}
        ${this._setupDevicesCard(plan, st)}
        ${this._setupActivityCard(st)}
      </div>`;
  },

  _setupNetworksCard(plan) {
    const rows = plan.networks
      .map(
        (n, i) => `<tr class="click" data-action="plan-net-edit" data-index="${i}" tabindex="0">
          <td><strong>${esc(n.name || n.id)}</strong></td><td class="num">${esc(n.vlan)}</td><td class="mono">${esc(n.subnet)}</td>
          <td class="hide-sm">${n.dhcp ? "DHCP" : "Static"}</td><td>${n.internet ? "Internet" : '<span class="muted">No internet</span>'}</td></tr>`,
      )
      .join("");
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Networks</h2>
        <button class="btn" data-action="plan-net-add">${icon("plus")}Add</button></div>
      ${plan.networks.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th class="num">VLAN</th><th>Router address</th><th class="hide-sm">Addresses</th><th>Internet</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No networks yet. Each network is a VLAN with its own addresses.</p>'}
    </div>`;
  },

  _setupAccessCard(plan) {
    const nets = plan.networks;
    if (!nets.length) return "";
    const head = nets.map((n) => `<th class="rot" title="${esc(n.name)}"><span>${esc(n.name || n.id)}</span></th>`).join("");
    const rows = nets
      .map((from) => {
        const cells = nets
          .map((to) =>
            to.id === from.id
              ? '<td class="mid muted" title="Inside a network everyone can talk to each other unless the Wi-Fi isolates clients">·</td>'
              : `<td class="mid"><input type="checkbox" aria-label="${esc(from.name)} can reach ${esc(to.name)}" data-plan="reach" data-from="${esc(from.id)}" data-to="${esc(to.id)}" ${(from.reach || []).includes(to.id) ? "checked" : ""}></td>`,
          )
          .join("");
        return `<tr><th scope="row">${esc(from.name || from.id)}</th>
          <td class="mid"><input type="checkbox" aria-label="${esc(from.name)} has internet" data-plan="internet" data-net="${esc(from.id)}" ${from.internet ? "checked" : ""}></td>
          <td class="mid"><input type="checkbox" aria-label="${esc(from.name)} can reach the router" data-plan="router_access" data-net="${esc(from.id)}" ${from.router_access ? "checked" : ""}></td>
          <td class="mid"><input type="checkbox" aria-label="${esc(from.name)} can reach the LAN" data-plan="reach" data-from="${esc(from.id)}" data-to="lan" ${(from.reach || []).includes("lan") ? "checked" : ""}></td>
          ${cells}</tr>`;
      })
      .join("");
    return `<div class="card panel-card wide"><div class="card-head"><h2 class="grow">Who can reach what</h2></div>
      <p class="muted" style="margin:0 0 10px;font-size:13px">A tick lets devices in the row's network start connections to the column. Replies are always allowed.
        Everything else is blocked. <em>Router</em> is the router's own web page and SSH (DHCP and DNS always work).</p>
      <div class="table-wrap"><table class="matrix"><thead><tr><th>From \\ To</th><th class="rot"><span>Internet</span></th><th class="rot"><span>Router</span></th><th class="rot"><span>Main LAN</span></th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
  },

  _setupWifiCard(plan) {
    const name = (id) => (plan.networks.find((n) => n.id === id) || {}).name || id;
    const rows = plan.wifi
      .map(
        (w, i) => `<tr class="click" data-action="plan-wifi-edit" data-index="${i}" tabindex="0">
          <td><strong>${esc(w.ssid)}</strong>${w.enabled === false ? ' <span class="chip">Off</span>' : ""}${w.hidden ? ' <span class="chip">Hidden</span>' : ""}${w.isolate ? ' <span class="chip">Isolated</span>' : ""}</td>
          <td>${esc(name(w.network))}</td><td>${esc(SECURITY_LABELS[w.security] || w.security)}</td><td class="hide-sm">${esc((w.bands || []).join(", "))}</td>
          <td class="num">${["wpa2", "wpa2_wpa3", "wpa3", "open"].includes(w.security) && (w.security === "open" || (this._setupState().state.secrets || []).includes(`wifi:${w.id}`)) ? `<button class="btn small" data-action="plan-wifi-qr" data-wifi="${esc(w.id)}" title="Show the password and a QR code to join">${icon("eye")}QR</button>` : ""}</td></tr>`,
      )
      .join("");
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Wi-Fi</h2>
        <button class="btn" data-action="plan-wifi-add" ${plan.networks.length ? "" : "disabled"}>${icon("plus")}Add</button></div>
      ${plan.wifi.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Network</th><th>Security</th><th class="hide-sm">Bands</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">${plan.networks.length ? "No Wi-Fi networks yet." : "Add a network first."}</p>`}
    </div>`;
  },

  _setupKeysCard(st) {
    const plan = st.plan; // keys are saved immediately: use the saved plan
    const ppsk = plan.wifi.filter((w) => w.security === "ppsk");
    const ssid = (id) => (plan.wifi.find((w) => w.id === id) || {}).ssid || id;
    const net = (id) => (id ? (plan.networks.find((n) => n.id === id) || {}).name || id : "");
    const rows = plan.keys
      .map(
        (k) => `<tr><td><strong>${esc(k.name)}</strong>${k.mac ? `<br><span class="mono muted">${esc(k.mac)}</span>` : ""}</td>
          <td>${esc(ssid(k.wifi))}${k.network ? `<br><span class="muted">→ ${esc(net(k.network))}</span>` : ""}</td>
          <td class="hide-sm">${k.rotation ? `<span class="chip">New key${k.rotation.cutoff ? ` · old until ${esc(new Date(k.rotation.cutoff).toLocaleDateString())}` : ""}</span>` : esc(timeLeft(k.expires))}${k.rotate_days && !k.rotation ? `<br><span class="muted">changes every ${esc(rotateLabel(k.rotate_days))}</span>` : ""}</td>
          <td class="num"><span class="row-actions">${k.rotation
            ? `<button class="btn small" data-action="plan-key-show-next" data-key="${esc(k.id)}">${icon("eye")}New key</button>
               <button class="btn small primary" data-action="plan-key-rotate-finish" data-key="${esc(k.id)}">${icon("check")}Device updated</button>
               <button class="btn small" data-action="plan-key-rotate-cancel" data-key="${esc(k.id)}">${icon("close")}Keep old</button>`
            : `<button class="btn small" data-action="plan-key-show" data-key="${esc(k.id)}">${icon("eye")}Key</button>
               ${k.expires ? "" : `<button class="btn small" data-action="plan-key-rotate" data-key="${esc(k.id)}">${icon("cycle")}New key</button>`}`}
            <button class="btn small danger" data-action="plan-key-revoke" data-key="${esc(k.id)}">${icon("trash")}Revoke</button></span></td></tr>`,
      )
      .join("");
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Devices &amp; keys</h2>
        <button class="btn" data-action="plan-key-add" ${ppsk.length ? "" : "disabled"}>${icon("plus")}Add device</button></div>
      ${plan.keys.length ? `<div class="table-wrap"><table><thead><tr><th>Device</th><th>Wi-Fi</th><th class="hide-sm">Valid</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">${ppsk.length ? "Give each device its own key: revoking one only disconnects that device." : "Save a Wi-Fi network with <em>Per-device keys</em> to hand out keys."}</p>`}
      <p class="muted" style="font-size:12px;margin:10px 0 0">Keys take effect straight away on the devices already set up (OpenWrt today). Timed keys are removed on their own, also after a restart.</p>
    </div>`;
  },

  _setupOnboardingCard(plan, st) {
    const ob = plan.onboarding || {};
    const portal = st.portal || {};
    const shared = plan.wifi.filter((w) => w.security !== "ppsk");
    const ppsk = plan.wifi.filter((w) => w.security === "ppsk");
    const waiting = (st.requests || []).filter((r) => r.status === "pending");
    const decided = (st.requests || []).filter((r) => r.status !== "pending").slice(0, 5);
    const ssid = (id) => (plan.wifi.find((w) => w.id === id) || {}).ssid || "";
    const status = portal.on
      ? `Running on the router at <span class="mono">${esc(portal.url)}</span>${portal.error ? ` · <span class="err-text">${esc(portal.error)}</span>` : ""}`
      : ob.enabled ? "Turned on: press Review &amp; apply to put it on the router." : "Off";
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Onboarding portal</h2>
        ${waiting.length ? `<span class="chip bad">${waiting.length} waiting</span>` : ""}</div>
      <p class="muted" style="margin:0 0 6px;font-size:13px">New devices join the onboarding Wi-Fi, open any web page and ask for access. You approve them here, and the portal shows the device its own password and QR code. Onboarding devices only talk to the router, never to Home Assistant.</p>
      <p class="muted" style="margin:0 0 4px;font-size:13px">${status}</p>
      ${waiting.length ? `<div class="table-wrap"><table><thead><tr><th>Device</th><th>Asks for</th><th></th></tr></thead><tbody>${waiting
        .map((r) => `<tr><td><strong>${esc(r.name)}</strong><br><span class="muted">${esc(r.kind_label || r.kind)} · <span class="mono">${esc(r.mac)}</span></span></td>
          <td>${esc(ssid(r.wants) || "Any")}</td>
          <td class="num"><span class="row-actions"><button class="btn small primary" data-action="plan-ob-approve" data-request="${esc(r.id)}">${icon("check")}Approve</button>
            <button class="btn small danger" data-action="plan-ob-decline" data-request="${esc(r.id)}">${icon("close")}Decline</button></span></td></tr>`)
        .join("")}</tbody></table></div>` : portal.on ? '<p class="muted" style="font-size:13px">No device is waiting.</p>' : ""}
      ${decided.length ? `<ul class="activity" style="margin-top:8px">${decided.map((r) => `<li><strong>${esc(r.name)}</strong> <span class="muted">${r.status === "approved" ? "approved" : "declined"}</span></li>`).join("")}</ul>` : ""}
      <div class="form-row"><label class="toggle"><input type="checkbox" data-plan="ob_enabled" ${ob.enabled ? "checked" : ""}><span class="track"></span><span>Portal on</span></label></div>
      <div class="form-row"><label class="section-title" for="ob-wifi">Onboarding Wi-Fi</label>
        <select id="ob-wifi" class="plan-select" data-plan="ob_wifi"><option value="">Choose…</option>${shared.map((w) => `<option value="${esc(w.id)}" ${ob.wifi === w.id ? "selected" : ""}>${esc(w.ssid)}</option>`).join("")}</select>
        <div class="hint">A Wi-Fi network with a shared password (or open), e.g. homeOB. Give it client isolation and a small client limit.</div></div>
      <div class="form-row"><span class="section-title">Devices may ask for</span>${ppsk.length ? `<div class="checks">${ppsk
        .map((w) => `<label><input type="checkbox" data-plan="ob_offer" data-wifi="${esc(w.id)}" ${(ob.offer || []).includes(w.id) ? "checked" : ""}>${esc(w.ssid)}</label>`)
        .join("")}</div>` : '<div class="hint">Add a Wi-Fi network with per-device keys first.</div>'}</div>
      <div class="form-row"><span class="section-title">Rules</span>
        <div class="muted" style="font-size:13px;margin-bottom:8px">${(ob.rules || []).length ? (ob.rules || []).map((r) => `${esc(kindLabel(ob.kinds, r.kind))} → ${esc(ssid(r.wifi))}${r.auto ? " (automatic)" : ""}`).join(" · ") : "None: you choose for every device"}</div>
        <button class="btn" data-action="plan-rules-edit">${icon("edit")}Edit rules</button></div>
      <div class="form-row"><label class="section-title" for="ob-hour">Requests per hour (all devices together)</label>
        <div class="field" style="max-width:160px"><input type="number" id="ob-hour" min="1" max="100" data-plan="ob_per_hour" value="${esc(ob.max_per_hour ?? 10)}"></div></div>
      <div class="form-row"><span class="section-title">Look and text</span>
        <div class="muted" style="font-size:13px;margin-bottom:8px">${esc(ob.title || "Welcome")} · ${ob.use_custom ? "your own page" : "standard page"}${ob.logo ? " · logo" : ""}</div>
        <button class="btn" data-action="plan-portal-edit">${icon("edit")}Customise portal</button></div>
    </div>`;
  },

  _portalForm(d) {
    if (!d.form) {
      const ob = this._setupState().draft.onboarding || {};
      const colors = ob.colors || {};
      d.form = {
        title: ob.title ?? "Welcome", text: ob.text ?? "", button: ob.button ?? "Ask for access",
        waiting_text: ob.waiting_text ?? "", approved_text: ob.approved_text ?? "", declined_text: ob.declined_text ?? "", footer: ob.footer ?? "",
        kinds: (ob.kinds || []).join("\n"), ask_network: ob.ask_network !== false, logo: ob.logo || null,
        color_accent: colors.accent || "#0a84c1", color_background: colors.background || "#f4f6f8", color_text: colors.text || "#1c1c1c", color_card: colors.card || "#ffffff",
        use_custom: Boolean(ob.use_custom), custom_html: ob.custom_html || "",
      };
    }
    return d.form;
  },

  _portalSettings(f) {
    return {
      title: f.title, text: f.text, button: f.button, waiting_text: f.waiting_text, approved_text: f.approved_text,
      declined_text: f.declined_text, footer: f.footer,
      kinds: String(f.kinds || "").split("\n").map((k) => k.trim()).filter(Boolean),
      ask_network: Boolean(f.ask_network), logo: f.logo || null,
      colors: { accent: f.color_accent, background: f.color_background, text: f.color_text, card: f.color_card },
      use_custom: Boolean(f.use_custom), custom_html: f.custom_html || "",
    };
  },

  async _portalPreview() {
    const d = this._dialog;
    if (!d || d.type !== "plan-portal") return;
    const r = await this._setupWs({ type: `${DOMAIN}/plan/portal_preview`, onboarding: this._portalSettings(this._portalForm(d)), page: d.page || "form" });
    if (!r || this._dialog !== d) return;
    d.previewHtml = r.html;
    const frame = this.shadowRoot.querySelector("iframe.portal-preview");
    if (frame) frame.srcdoc = r.html; // in place: doesn't disturb a field being typed in
    else this._render();
  },

  _planPortalDialog(d) {
    const f = this._portalForm(d);
    const area = (id, label, field, rows, hint = "") => `<div class="form-row"><label class="section-title" for="${id}">${esc(label)}</label>
      <textarea id="${id}" data-field="${field}" rows="${rows}" spellcheck="true">${esc(f[field] ?? "")}</textarea>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
    const color = (key, label) => `<label class="color-pick"><input type="color" data-field="color_${key}" value="${esc(f[`color_${key}`])}"><span>${esc(label)}</span></label>`;
    const pages = [["form", "Ask"], ["waiting", "Waiting"], ["approved", "Approved"], ["declined", "Declined"]];
    return `<div class="dialog portal-dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead("Customise the portal", "What new devices see on the onboarding Wi-Fi")}
      <div class="portal-grid">
        <div class="portal-settings">
          <h3>Text</h3>
          ${this._textRow("pt-title", "Title", "title", f.title, {})}
          ${area("pt-text", "Welcome text", "text", 2)}
          ${this._textRow("pt-button", "Button", "button", f.button, {})}
          ${area("pt-waiting", "While waiting", "waiting_text", 2)}
          ${area("pt-approved", "When approved", "approved_text", 2, "<span class=\"mono\">{device}</span> and <span class=\"mono\">{network}</span> are filled in. The password and QR code follow.")}
          ${area("pt-declined", "When declined", "declined_text", 2)}
          ${area("pt-footer", "Footer", "footer", 2, "E.g. house rules or who to ask. Shown on every page.")}
          <h3>Look</h3>
          <div class="form-row"><span class="section-title">Logo</span>
            <div class="logo-row">${f.logo ? `<img src="${esc(f.logo)}" alt="Logo" class="logo-thumb"><button class="btn small" data-action="plan-portal-logo-remove">${icon("trash")}Remove</button>` : '<span class="muted">None</span>'}
              <label class="btn small file-btn">${icon("plus")}${f.logo ? "Replace" : "Choose image"}<input type="file" accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml" data-logo></label></div>
            <div class="hint">PNG, JPEG, GIF, WebP or SVG, up to 70 KB. It's built into the page (the onboarding network has no internet).</div></div>
          <div class="form-row"><span class="section-title">Colours</span><div class="colors">
            ${color("accent", "Button")}${color("background", "Background")}${color("text", "Text")}${color("card", "Fields")}</div></div>
          <h3>Questions</h3>
          ${area("pt-kinds", "Kinds of device (one per line)", "kinds", 4, "Up to 12. The last one is used when a device picks none.")}
          ${this._toggleRow("ask_network", "Let devices choose the network (from the ones offered)", f.ask_network)}
          <h3>Your own page</h3>
          ${this._toggleRow("use_custom", "Use my own page (HTML)", f.use_custom, true)}
          ${f.use_custom ? `${area("pt-html", "Page", "custom_html", 12, "Put <span class=\"mono\">{{content}}</span> where the form, waiting message or password go (required, once). Optional: <span class=\"mono\">{{title}}</span>, <span class=\"mono\">{{style}}</span> (the colours above), <span class=\"mono\">{{logo}}</span>, <span class=\"mono\">{{footer}}</span>. Everything must be inside the page: the onboarding network has no internet.")}
            <button class="btn small" data-action="plan-portal-default">${icon("restore")}Start from the standard page</button>` : ""}
        </div>
        <div class="portal-preview-col">
          <div class="filters" role="tablist">${pages.map(([k, label]) => `<button class="filter" role="tab" data-action="plan-portal-page" data-page="${k}" aria-pressed="${(d.page || "form") === k}">${label}</button>`).join("")}
            <button class="filter" data-action="plan-portal-refresh" title="Update the preview">${icon("reboot")}</button></div>
          <iframe class="portal-preview" sandbox="" title="Portal preview" srcdoc="${esc(d.previewHtml || "<p style='font-family:sans-serif;color:#888'>Loading preview…</p>")}"></iframe>
          <div class="hint">The preview updates when you leave a field.</div>
        </div>
      </div>
      <div class="dlg-actions"><button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-portal-done">${icon("check")}Done</button></div></div>`;
  },

  _planRotateDialog(d) {
    const st = this._setupState().state;
    const key = st.plan.keys.find((k) => k.id === d.key);
    if (!key) return "";
    const f = (d.form = d.form || {});
    const wifi = st.plan.wifi.find((w) => w.id === key.wifi) || {};
    const siblings = st.plan.keys.filter((k) => k.wifi === key.wifi && !k.rotation && !k.expires);
    const busy = this._isBusy("plan-key-rotate");
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(`New key for ${key.name}`, wifi.ssid ? `Wi-Fi: ${wifi.ssid}` : "")}
      <p class="muted" style="margin-top:0">A new key is made and works alongside the old one, so nothing drops off. Put the new key on the device, then press <strong>Device updated</strong> and the old key stops.</p>
      <div class="form-row"><label class="section-title" for="rot-cutoff">The old key works</label>
        <select id="rot-cutoff" class="plan-select" data-field="cutoff_days">${CUTOFF_CHOICES.map(([dd, label]) => `<option value="${dd}" ${Number(f.cutoff_days ?? key.cutoff_days ?? 0) === dd ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      ${siblings.length > 1 ? this._toggleRow("all", `All ${siblings.length} keys on ${wifi.ssid || "this network"}`, Boolean(f.all)) : ""}
      <div class="dlg-actions"><button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-key-rotate-start" ${busy ? "disabled" : ""}>${icon("cycle")}${busy ? "Working…" : "Make new key"}</button></div></div>`;
  },

  _planRulesDialog(d) {
    const s = this._setupState();
    const plan = s.draft;
    const ob = plan.onboarding || {};
    const kinds = kindChoices(ob.kinds);
    const ppsk = plan.wifi.filter((w) => w.security === "ppsk");
    const rules = (d.rules = d.rules || clone(ob.rules || []));
    const row = (r, i) => `<div class="rule-row">
        <select class="plan-select" data-rule="${i}" data-rule-field="kind" aria-label="Kind of device">${kinds.map(([id, label]) => `<option value="${esc(id)}" ${r.kind === id ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        <select class="plan-select" data-rule="${i}" data-rule-field="wifi" aria-label="Wi-Fi network">${ppsk.map((w) => `<option value="${esc(w.id)}" ${r.wifi === w.id ? "selected" : ""}>${esc(w.ssid)}</option>`).join("")}</select>
        <select class="plan-select" data-rule="${i}" data-rule-field="network" aria-label="Network"><option value="">Its own network</option>${plan.networks.map((n) => `<option value="${esc(n.id)}" ${r.network === n.id ? "selected" : ""}>${esc(n.name)}</option>`).join("")}</select>
        <select class="plan-select" data-rule="${i}" data-rule-field="minutes" aria-label="Valid for">${KEY_DURATIONS.map(([m, label]) => `<option value="${m}" ${Number(r.minutes || 0) === m ? "selected" : ""}>${label}</option>`).join("")}</select>
        <label class="toggle"><input type="checkbox" data-rule="${i}" data-rule-field="auto" ${r.auto ? "checked" : ""}><span class="track"></span><span>Approve automatically</span></label>
        <button class="icon-btn" data-action="plan-rule-remove" data-rule="${i}" aria-label="Remove rule">${icon("trash")}</button></div>`;
    return `<div class="dialog wide-dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead("Rules for kinds of device", "What each kind of device gets when you approve it")}
      ${rules.length ? `<div class="rules">${rules.map(row).join("")}</div>` : '<p class="muted">No rules: you choose the network for every device.</p>'}
      ${ppsk.length ? `<button class="btn small" data-action="plan-rule-add">${icon("plus")}Add rule</button>` : '<p class="hint">Add a Wi-Fi network with per-device keys first.</p>'}
      <p class="hint">With a rule, the approve dialog starts with its choices. <strong>Approve automatically</strong> lets every device of that kind in without asking you: anyone with the onboarding password can then pick that kind, so use it for networks that are isolated (e.g. TVs on homeISO).</p>
      <div class="dlg-actions"><button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-rules-done">${icon("check")}Done</button></div></div>`;
  },

  _planApproveDialog(d) {
    const st = this._setupState().state;
    const req = (st.requests || []).find((r) => r.id === d.request);
    if (!req) return `<div class="dialog" role="dialog" aria-modal="true"><p>This request is no longer waiting.</p><div class="dlg-actions"><button class="btn" data-action="close">Close</button></div></div>`;
    const plan = st.plan;
    const f = (d.form = d.form || {});
    const offered = plan.wifi.filter((w) => w.security === "ppsk");
    const sug = req.suggested || {};
    const wifi = f.wifi || (offered.some((w) => w.id === sug.wifi) ? sug.wifi : offered.some((w) => w.id === req.wants) ? req.wants : offered[0] && offered[0].id);
    if (f.network === undefined && sug.network) f.network = sug.network;
    if (f.minutes === undefined && sug.minutes) f.minutes = String(sug.minutes);
    const minutes = Number(f.minutes ?? 0);
    const busy = this._isBusy("plan-ob-approve");
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(`Approve ${req.name}`, `${req.mac}${req.ip ? ` · ${req.ip}` : ""}`)}
      <div class="form-row" style="margin-top:0"><label class="section-title" for="ap-wifi">Wi-Fi network</label>
        <select id="ap-wifi" class="plan-select" data-field="wifi">${offered.map((w) => `<option value="${esc(w.id)}" ${wifi === w.id ? "selected" : ""}>${esc(w.ssid)}${w.id === req.wants ? " (asked for)" : ""}</option>`).join("")}</select></div>
      <div class="form-row"><label class="section-title" for="ap-net">Put it in</label>
        <select id="ap-net" class="plan-select" data-field="network"><option value="">The Wi-Fi's own network</option>${plan.networks.map((n) => `<option value="${esc(n.id)}" ${f.network === n.id ? "selected" : ""}>${esc(n.name)} · VLAN ${esc(n.vlan)}</option>`).join("")}</select></div>
      <div class="form-row"><span class="section-title">Valid for</span><div class="durations" role="radiogroup">${KEY_DURATIONS.map(([m, label]) => `<label class="choice"><input type="radio" name="ap-duration" data-field="minutes" value="${m}" ${m === minutes ? "checked" : ""}><span>${label}</span></label>`).join("")}</div></div>
      ${this._toggleRow("bind", "Only for this device's MAC address", Boolean(f.bind))}
      <div class="hint">Phones and laptops often use a different (private) address on each Wi-Fi network; leave this off for them.</div>
      <div class="dlg-actions"><button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-ob-approve-confirm" ${busy || !wifi ? "disabled" : ""}>${icon("check")}${busy ? "Approving…" : "Approve"}</button></div></div>`;
  },

  _setupDevicesCard(plan, st) {
    const devices = st.devices;
    const routers = devices.filter((d) => d.router_capable);
    const aps = devices.filter((d) => d.wireless && !(d.entry_id === plan.devices.router));
    const switches = devices.filter((d) => d.kind === "exos" || d.kind === "switch");
    const apOn = (id) => plan.devices.aps == null || plan.devices.aps.includes(id);
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Devices</h2></div>
      <div class="form-row" style="margin-top:0"><label class="section-title" for="plan-router">Router (addresses, DHCP, firewall)</label>
        <select id="plan-router" class="plan-select" data-plan="router"><option value="">None: my router is set up separately</option>
          ${routers.map((d) => `<option value="${esc(d.entry_id)}" ${plan.devices.router === d.entry_id ? "selected" : ""}>${esc(d.name)} (${esc(d.host)})</option>`).join("")}</select>
        ${routers.length ? "" : '<div class="hint">No OpenWrt device added yet.</div>'}</div>
      ${plan.devices.router ? `<div class="form-row"><label class="section-title" for="plan-trunks">Router ports to the switch (carry every VLAN, tagged)</label>
        <div class="field"><input type="text" id="plan-trunks" data-plan="router_trunks" value="${esc((plan.devices.router_trunks || []).join(", "))}" placeholder="br-lan" spellcheck="false"></div>
        <div class="hint">Interface names, e.g. <span class="mono">br-lan</span> or <span class="mono">eth1</span>.</div></div>` : ""}
      ${switches.map((d) => `<div class="form-row"><label class="section-title" for="trunk-${esc(d.entry_id)}">${esc(d.name)}: trunk ports (to router and access points)</label>
        <div class="field"><input type="text" id="trunk-${esc(d.entry_id)}" data-plan="switch_trunk" data-entry="${esc(d.entry_id)}" value="${esc((plan.devices.switch_trunks || {})[d.entry_id] || "")}" placeholder="${d.kind === "switch" ? "e.g. 9-10" : "e.g. 47-48"}" spellcheck="false"></div>
        <div class="hint">Port numbers like <span class="mono">47-48</span>${d.kind === "switch" ? " (or <span class=\"mono\">1/0/9</span>)" : ""}.</div></div>`).join("")}
      ${aps.length ? `<div class="form-row"><span class="section-title">Access points that broadcast the Wi-Fi</span><div class="checks">
        ${aps.map((d) => `<label><input type="checkbox" data-plan="ap" data-entry="${esc(d.entry_id)}" ${apOn(d.entry_id) ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div></div>` : ""}
      ${devices.filter((d) => d.note).map((d) => `<div class="hint">${esc(d.name)}: ${esc(d.note)}</div>`).join("")}
    </div>`;
  },

  _setupActivityCard(st) {
    if (!st.audit.length) return "";
    const labels = {
      plan_saved: "Setup saved", applied: "Applied", key_added: "Key added", key_revoked: "Key revoked", key_expired: "Key expired",
      onboarding_request: "Device asked to join", onboarding_approved: "Device approved", onboarding_declined: "Device declined", onboarding_expired: "Request expired",
    };
    return `<div class="card panel-card"><div class="card-head"><h2 class="grow">Activity</h2></div>
      <ul class="activity">${st.audit.slice(0, 12).map((a) => `<li><span class="muted">${esc(new Date(a.time).toLocaleString())}</span> <strong>${esc(labels[a.action] || a.action)}</strong> ${esc(a.detail || "")}${a.user ? ` <span class="muted">· ${esc(a.user)}</span>` : ""}</li>`).join("")}</ul></div>`;
  },

  /* ---------- page inputs (matrix, devices) ---------- */

  _planInput(target) {
    const s = this._setupState();
    const plan = s.draft;
    const kind = target.dataset.plan;
    const net = (id) => plan.networks.find((n) => n.id === id);
    if (kind === "reach") {
      const from = net(target.dataset.from);
      const reach = new Set(from.reach || []);
      if (target.checked) reach.add(target.dataset.to);
      else reach.delete(target.dataset.to);
      from.reach = [...reach];
    } else if (kind === "internet" || kind === "router_access") {
      net(target.dataset.net)[kind] = target.checked;
    } else if (kind === "router") {
      plan.devices.router = target.value || null;
    } else if (kind === "router_trunks") {
      plan.devices.router_trunks = target.value.split(/[,\s]+/).filter(Boolean);
    } else if (kind === "switch_trunk") {
      plan.devices.switch_trunks = { ...(plan.devices.switch_trunks || {}), [target.dataset.entry]: target.value.trim() };
    } else if (kind.startsWith("ob_")) {
      const ob = (plan.onboarding = { ...(plan.onboarding || {}) });
      if (kind === "ob_enabled") ob.enabled = target.checked;
      else if (kind === "ob_wifi") ob.wifi = target.value || null;
      else if (kind === "ob_title") ob.title = target.value;
      else if (kind === "ob_text") ob.text = target.value;
      else if (kind === "ob_per_hour") ob.max_per_hour = Math.max(1, Math.min(100, Number(target.value) || 10));
      else if (kind === "ob_offer") {
        const offer = new Set(ob.offer || []);
        if (target.checked) offer.add(target.dataset.wifi);
        else offer.delete(target.dataset.wifi);
        ob.offer = [...offer];
      }
    } else if (kind === "ap") {
      const all = s.state.devices.filter((d) => d.wireless).map((d) => d.entry_id);
      const on = new Set(plan.devices.aps == null ? all : plan.devices.aps);
      if (target.checked) on.add(target.dataset.entry);
      else on.delete(target.dataset.entry);
      plan.devices.aps = all.every((id) => on.has(id)) ? null : [...on];
    }
    this._setupDirty(target.type !== "text");
  },

  /* ---------- dialogs ---------- */

  _planDialog(d) {
    if (d.type === "plan-net") return this._planNetDialog(d);
    if (d.type === "plan-wifi") return this._planWifiDialog(d);
    if (d.type === "plan-key-add") return this._planKeyAddDialog(d);
    if (d.type === "plan-key-show") return this._planKeyShowDialog(d);
    if (d.type === "plan-apply") return this._planApplyDialog(d);
    if (d.type === "plan-approve") return this._planApproveDialog(d);
    if (d.type === "plan-portal") return this._planPortalDialog(d);
    if (d.type === "plan-rotate") return this._planRotateDialog(d);
    if (d.type === "plan-rules") return this._planRulesDialog(d);
    return "";
  },

  _dlgHead(title, sub = "") {
    return `<div class="dlg-head"><div class="grow"><h2 id="dlg-title">${esc(title)}</h2>${sub ? `<div class="muted">${esc(sub)}</div>` : ""}</div>
      <button class="icon-btn" data-action="close" aria-label="Close">${icon("close")}</button></div>`;
  },

  _textRow(id, label, field, value, { type = "text", placeholder = "", hint = "", min, max } = {}) {
    return `<div class="form-row"><label class="section-title" for="${id}">${esc(label)}</label>
      <div class="field"><input type="${type}" id="${id}" data-field="${field}" value="${esc(value ?? "")}" placeholder="${esc(placeholder)}" ${min != null ? `min="${min}"` : ""} ${max != null ? `max="${max}"` : ""} autocomplete="off" spellcheck="false"></div>
      ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  },

  _toggleRow(field, label, checked, rerender = false) {
    return `<div class="form-row"><label class="toggle"><input type="checkbox" data-field="${field}" ${rerender ? "data-rerender" : ""} ${checked ? "checked" : ""}><span class="track"></span><span>${esc(label)}</span></label></div>`;
  },

  _planNetDialog(d) {
    const plan = this._setupState().draft;
    const net = d.index != null ? plan.networks[d.index] : null;
    const f = (d.form = d.form || {});
    const v = (key, fallback) => (f[key] !== undefined ? f[key] : net ? net[key] : fallback);
    const switches = this._setupState().state.devices.filter((x) => x.kind === "exos" || x.kind === "switch");
    const dhcp = v("dhcp", true);
    return `<div class="dialog wide-dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(net ? net.name || net.id : "Add a network", net ? `VLAN ${net.vlan} · ID ${net.id}` : "A VLAN with its own addresses")}
      ${this._textRow("net-name", "Name", "name", v("name", ""), { placeholder: "e.g. IoT" })}
      <div class="two">
        ${this._textRow("net-vlan", "VLAN ID", "vlan", v("vlan", ""), { type: "number", min: 2, max: 4094 })}
        ${this._textRow("net-subnet", "Router address", "subnet", v("subnet", ""), { placeholder: "10.30.0.1/24" })}
      </div>
      ${this._toggleRow("dhcp", "Hand out addresses (DHCP)", dhcp, true)}
      ${dhcp ? `<div class="three">
        ${this._textRow("net-start", "First address", "dhcp_start", v("dhcp_start", 50), { type: "number", min: 1, hint: "e.g. 50 = .50" })}
        ${this._textRow("net-limit", "How many", "dhcp_limit", v("dhcp_limit", 150), { type: "number", min: 1 })}
        ${this._textRow("net-lease", "Lease", "lease", v("lease", "12h"), { placeholder: "12h" })}
      </div>` : ""}
      ${this._toggleRow("internet", "Internet access", v("internet", true))}
      ${this._toggleRow("router_access", "May open the router's web page and SSH", v("router_access", false))}
      ${switches.map((sw) => this._textRow(`net-ports-${sw.entry_id}`, `${sw.name}: ports for devices in this network (untagged)`, `ports:${sw.entry_id}`, f[`ports:${sw.entry_id}`] ?? ((net && net.switch_ports) || {})[sw.entry_id] ?? "", { placeholder: "e.g. 5-8,12" })).join("")}
      <div class="dlg-actions">
        ${net ? `<button class="btn danger" data-action="plan-net-delete" style="margin-right:auto">${icon("trash")}Delete</button>` : ""}
        <button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-net-done">${icon("check")}${net ? "Done" : "Add"}</button>
      </div></div>`;
  },

  _planWifiDialog(d) {
    const s = this._setupState();
    const plan = s.draft;
    const wifi = d.index != null ? plan.wifi[d.index] : null;
    const f = (d.form = d.form || {});
    const v = (key, fallback) => (f[key] !== undefined ? f[key] : wifi ? wifi[key] : fallback);
    const security = v("security", "wpa2");
    const needsPassword = ["wpa2", "wpa2_wpa3", "wpa3"].includes(security);
    const saved = wifi && s.state.secrets.includes(`wifi:${wifi.id}`);
    const bands = v("bands", ["2.4 GHz", "5 GHz"]);
    return `<div class="dialog wide-dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(wifi ? wifi.ssid : "Add a Wi-Fi network")}
      ${this._textRow("pw-ssid", "Network name (SSID)", "ssid", v("ssid", ""), { placeholder: "e.g. home" })}
      <div class="form-row"><label class="section-title" for="pw-network">Network (VLAN)</label>
        <select id="pw-network" class="plan-select" data-field="network">${plan.networks.map((n) => `<option value="${esc(n.id)}" ${v("network", plan.networks[0] && plan.networks[0].id) === n.id ? "selected" : ""}>${esc(n.name)} · VLAN ${esc(n.vlan)}</option>`).join("")}</select></div>
      <div class="form-row"><label class="section-title" for="pw-security">Security</label>
        <select id="pw-security" class="plan-select" data-field="security" data-rerender>${Object.entries(SECURITY_LABELS).map(([k, label]) => `<option value="${k}" ${security === k ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        <div class="hint">${security === "ppsk" ? "Every device gets its own key (Devices &amp; keys), and can be put in another network. Works on OpenWrt; Aerohive access points skip this network for now." : security === "open" ? "Anyone nearby can join." : security === "wpa3" ? "Older devices can't join WPA3-only networks." : ""}</div></div>
      ${needsPassword ? `<div class="form-row"><label class="section-title" for="plan-password">Password</label>
        <div class="field"><input type="${f.showPassword ? "text" : "password"}" id="plan-password" data-field="password" value="${esc(f.password || "")}" autocomplete="new-password" spellcheck="false" placeholder="${saved ? "Leave empty to keep the saved one" : "8–63 characters"}">
          <button class="icon-btn" data-action="wifi-show-password" aria-label="Show password" title="Show password">${icon("eye")}</button></div>
        <div class="hint">Stored encrypted in Home Assistant${saved ? " · a password is saved" : ""}.</div></div>` : ""}
      <div class="form-row"><span class="section-title">Bands</span><div class="checks">${PLAN_BANDS.map((b) => `<label><input type="checkbox" data-field="bands" data-multi value="${esc(b)}" ${bands.includes(b) ? "checked" : ""}>${esc(b)}</label>`).join("")}</div></div>
      ${this._toggleRow("enabled", "Broadcast this network", v("enabled", true))}
      ${this._toggleRow("hidden", "Hide the network name", v("hidden", false))}
      ${this._toggleRow("isolate", "Isolate clients (devices can't see each other)", v("isolate", false))}
      ${this._textRow("pw-max", "Max clients per access point", "max_clients", v("max_clients", "") ?? "", { type: "number", min: 1, max: 255, hint: "Empty: no limit" })}
      <div class="dlg-actions">
        ${wifi ? `<button class="btn danger" data-action="plan-wifi-delete" style="margin-right:auto">${icon("trash")}Delete</button>` : ""}
        <button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-wifi-done">${icon("check")}${wifi ? "Done" : "Add"}</button>
      </div></div>`;
  },

  _planKeyAddDialog(d) {
    const plan = this._setupState().state.plan;
    const f = (d.form = d.form || {});
    const ppsk = plan.wifi.filter((w) => w.security === "ppsk");
    const minutes = Number(f.minutes ?? 0);
    const busy = this._isBusy("plan-key-add");
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead("Add a device", "It gets its own Wi-Fi key")}
      ${this._textRow("key-name", "Device name", "name", f.name || "", { placeholder: "e.g. Living room TV" })}
      <div class="form-row"><label class="section-title" for="key-wifi">Wi-Fi network</label>
        <select id="key-wifi" class="plan-select" data-field="wifi">${ppsk.map((w) => `<option value="${esc(w.id)}" ${(f.wifi || ppsk[0].id) === w.id ? "selected" : ""}>${esc(w.ssid)}</option>`).join("")}</select></div>
      <div class="form-row"><label class="section-title" for="key-net">Put it in</label>
        <select id="key-net" class="plan-select" data-field="network"><option value="">The Wi-Fi's own network</option>${plan.networks.map((n) => `<option value="${esc(n.id)}" ${f.network === n.id ? "selected" : ""}>${esc(n.name)} · VLAN ${esc(n.vlan)}</option>`).join("")}</select></div>
      <div class="form-row"><span class="section-title">Valid for</span><div class="durations" role="radiogroup">${KEY_DURATIONS.map(([m, label]) => `<label class="choice"><input type="radio" name="key-duration" data-field="minutes" value="${m}" ${m === minutes ? "checked" : ""}><span>${label}</span></label>`).join("")}</div></div>
      ${minutes ? "" : `<div class="two"><div class="form-row"><label class="section-title" for="key-rotate">Change the key every</label>
        <select id="key-rotate" class="plan-select" data-field="rotate_days"><option value="">Never</option>${ROTATE_CHOICES.map(([d, label]) => `<option value="${d}" ${Number(f.rotate_days) === d ? "selected" : ""}>${label}</option>`).join("")}</select></div>
        <div class="form-row"><label class="section-title" for="key-cutoff">Old key then works</label>
        <select id="key-cutoff" class="plan-select" data-field="cutoff_days">${CUTOFF_CHOICES.map(([d, label]) => `<option value="${d}" ${Number(f.cutoff_days || 0) === d ? "selected" : ""}>${label}</option>`).join("")}</select></div></div>
        <div class="hint">A new key is made on schedule; you get a reminder to put it on the device. The old key keeps working meanwhile.</div>`}
      ${this._textRow("key-mac", "Only for this device's MAC address (optional)", "mac", f.mac || "", { placeholder: "aa:bb:cc:dd:ee:ff" })}
      ${this._textRow("key-key", "Key (optional)", "key", f.key || "", { placeholder: "Leave empty to make one up", hint: "8–63 characters." })}
      <div class="dlg-actions"><button class="btn" data-action="close">Cancel</button>
        <button class="btn primary" data-action="plan-key-create" ${busy ? "disabled" : ""}>${icon("plus")}${busy ? "Adding…" : "Add device"}</button></div></div>`;
  },

  _planKeyShowDialog(d) {
    return `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(d.name, d.ssid ? `Wi-Fi: ${d.ssid}` : "")}
      <div class="key-box mono" id="key-value">${esc(d.value)}</div>
      ${d.qr ? `<div class="qr-box" aria-label="QR code to join ${esc(d.ssid || "")}">${d.qr}</div>` : ""}
      <p class="muted" style="font-size:13px">${d.wifiOnly ? `The password of ${esc(d.ssid)}.` : d.upcoming ? `Put this key on ${esc(d.device)}; the old one keeps working until you press <strong>Device updated</strong>.` : `Enter this as the Wi-Fi password on ${esc(d.name)}.`} ${d.qr ? "Or scan the code with the device's camera to join." : ""} ${d.expires ? `It stops working in ${esc(timeLeft(d.expires).replace(" left", ""))}.` : ""}</p>
      <div class="dlg-actions"><button class="btn" data-action="plan-copy">${icon("save")}Copy</button><button class="btn primary" data-action="close">Done</button></div></div>`;
  },

  _planApplyDialog(d) {
    const busy = this._isBusy("plan-apply");
    let body = '<p class="muted">Working out the changes…</p>';
    if (d.results) {
      body = d.results
        .map((r) => `<div class="apply-row"><span class="chip ${r.ok ? "ok" : "bad"}">${r.ok ? "Done" : "Failed"}</span> <strong>${esc(r.name)}</strong> <span class="muted">${esc(ROLE_LABELS[r.role] || r.role)}</span>
          ${r.error ? `<div class="hint err">${esc(r.error)}</div>` : ""}${(r.warnings || []).map((w) => `<div class="hint">${esc(w)}</div>`).join("")}</div>`)
        .join("");
    } else if (d.preview) {
      const p = d.preview;
      body = `${p.errors.length ? `<div class="banner err">${icon("warn")}<div class="grow"><ul class="plan-errors">${p.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div></div>` : ""}
        ${(p.notes || []).map((n) => `<p class="hint">${esc(n)}</p>`).join("")}
        ${p.devices.length ? p.devices
          .map((x) => `<div class="apply-row"><strong>${esc(x.name)}</strong> <span class="muted">${esc(ROLE_LABELS[x.role] || x.role)}${x.online ? "" : " · offline"}</span>
            ${x.errors.map((e) => `<div class="hint err">${esc(e)}</div>`).join("")}${x.warnings.map((w) => `<div class="hint">${esc(w)}</div>`).join("")}
            <details><summary>${x.commands.length} command${x.commands.length === 1 ? "" : "s"}</summary><pre class="cmds">${esc(x.commands.join("\n"))}</pre></details></div>`)
          .join("") : '<p class="muted">No device to set up: add an OpenWrt router, an access point or an ExtremeXOS switch.</p>'}
        <p class="hint">Switches go first, then the router, then the access points. An OpenWrt device goes back to its previous settings on its own if Home Assistant can't reach it within 90 seconds of the change.</p>`;
    }
    const canApply = d.preview && !d.results && !d.preview.errors.length && d.preview.devices.length;
    return `<div class="dialog wide-dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
      ${this._dlgHead(d.results ? "Applied" : "Review & apply")}
      ${body}
      <div class="dlg-actions"><button class="btn" data-action="close">${d.results ? "Close" : "Cancel"}</button>
        ${canApply ? `<button class="btn primary" data-action="plan-apply" ${busy ? "disabled" : ""}>${icon("check")}${busy ? "Applying… (up to a minute)" : "Apply to the devices"}</button>` : ""}</div></div>`;
  },

  /* ---------- actions ---------- */

  async _planSave() {
    const s = this._setupState();
    const result = await this._setupWs({ type: `${DOMAIN}/plan/save`, plan: s.draft, secrets: s.secrets }, "plan-save");
    if (!result) return false;
    s.dirty = false;
    s.secrets = {};
    this._setupTake(result);
    this._toast(result.errors.length ? `Setup saved · ${result.errors.length} thing${result.errors.length === 1 ? "" : "s"} to fix before applying` : "Setup saved");
    this._render();
    return true;
  },

  _planAction(action, el) {
    const s = this._setupState();
    const d = this._dialog;
    const plan = s.draft;
    const num = (value) => (value === "" || value == null ? null : Number(value));
    switch (action) {
      case "plan-template":
        this._setupWs({ type: `${DOMAIN}/plan/template` }, "plan-template").then((r) => {
          if (!r) return;
          s.draft = { ...r.plan, keys: s.draft.keys };
          this._setupDirty();
          this._toast("Home layout loaded: check it, set the Wi-Fi passwords, then Save");
        });
        break;
      case "plan-save":
        this._planSave();
        break;
      case "plan-net-add":
        this._openDialog({ type: "plan-net", index: null, focus: "#net-name" });
        break;
      case "plan-net-edit":
        this._openDialog({ type: "plan-net", index: Number(el.dataset.index) });
        break;
      case "plan-net-done": {
        const f = d.form || {};
        const old = d.index != null ? plan.networks[d.index] : null;
        const get = (k, fallback) => (f[k] !== undefined ? f[k] : old ? old[k] : fallback);
        const name = String(get("name", "")).trim();
        const vlan = num(get("vlan", null));
        const subnet = String(get("subnet", "")).trim();
        if (!name) return this._toast("Give the network a name", true);
        if (!Number.isInteger(vlan) || vlan < 2 || vlan > 4094) return this._toast("The VLAN ID must be 2–4094", true);
        if (plan.networks.some((n, i) => n.vlan === vlan && i !== d.index)) return this._toast(`VLAN ${vlan} is already used`, true);
        if (!/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(subnet)) return this._toast("Enter the router's address with the size, e.g. 10.30.0.1/24", true);
        const ports = { ...((old && old.switch_ports) || {}) };
        for (const [k, value] of Object.entries(f)) if (k.startsWith("ports:")) ports[k.slice(6)] = String(value).trim();
        const net = {
          ...(old || { reach: [] }),
          id: old ? old.id : slugId(name, plan.networks.map((n) => n.id).concat(["lan"])),
          name,
          vlan,
          subnet,
          dhcp: Boolean(get("dhcp", true)),
          dhcp_start: num(get("dhcp_start", 50)) ?? 50,
          dhcp_limit: num(get("dhcp_limit", 150)) ?? 150,
          lease: String(get("lease", "12h")).trim() || "12h",
          internet: Boolean(get("internet", true)),
          router_access: Boolean(get("router_access", false)),
          switch_ports: ports,
        };
        if (old) plan.networks[d.index] = net;
        else plan.networks.push(net);
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-net-delete": {
        const net = plan.networks[d.index];
        const used = plan.wifi.filter((w) => w.network === net.id).map((w) => w.ssid);
        if (used.length) return this._toast(`${net.name} is used by ${used.join(", ")}: change those first`, true);
        plan.networks.splice(d.index, 1);
        for (const n of plan.networks) n.reach = (n.reach || []).filter((r) => r !== net.id);
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-wifi-add":
        this._openDialog({ type: "plan-wifi", index: null, focus: "#pw-ssid" });
        break;
      case "plan-wifi-edit":
        this._openDialog({ type: "plan-wifi", index: Number(el.dataset.index) });
        break;
      case "plan-wifi-done": {
        const f = d.form || {};
        const old = d.index != null ? plan.wifi[d.index] : null;
        const get = (k, fallback) => (f[k] !== undefined ? f[k] : old ? old[k] : fallback);
        const ssid = String(get("ssid", "")).trim();
        const security = get("security", "wpa2");
        const bands = get("bands", ["2.4 GHz", "5 GHz"]);
        if (!ssid || ssid.length > 32) return this._toast("The network name needs 1–32 characters", true);
        if (plan.wifi.some((w, i) => w.ssid === ssid && i !== d.index)) return this._toast(`${ssid} already exists`, true);
        if (!bands.length) return this._toast("Choose at least one band", true);
        const id = old ? old.id : slugId(ssid, plan.wifi.map((w) => w.id));
        const needsPassword = ["wpa2", "wpa2_wpa3", "wpa3"].includes(security);
        if (f.password && (f.password.length < 8 || f.password.length > 63)) return this._toast("A Wi-Fi password needs 8 to 63 characters", true);
        if (needsPassword && !f.password && !s.secrets[`wifi:${id}`] && !(s.state.secrets || []).includes(`wifi:${id}`)) return this._toast("Enter a password", true);
        const max = num(get("max_clients", null));
        const wifi = {
          ...(old || {}),
          id,
          ssid,
          network: get("network", plan.networks[0].id),
          security,
          bands,
          enabled: Boolean(get("enabled", true)),
          hidden: Boolean(get("hidden", false)),
          isolate: Boolean(get("isolate", false)),
          max_clients: max,
        };
        if (f.password) s.secrets[`wifi:${id}`] = f.password;
        if (old) plan.wifi[d.index] = wifi;
        else plan.wifi.push(wifi);
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-wifi-delete": {
        const wifi = plan.wifi[d.index];
        plan.wifi.splice(d.index, 1);
        delete s.secrets[`wifi:${wifi.id}`];
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-key-add":
        if (s.dirty) return this._toast("Save the setup first", true);
        this._openDialog({ type: "plan-key-add", focus: "#key-name" });
        break;
      case "plan-key-create": {
        const f = d.form || {};
        const ppsk = s.state.plan.wifi.filter((w) => w.security === "ppsk");
        const msg = { type: `${DOMAIN}/plan/key_add`, name: (f.name || "").trim(), wifi: f.wifi || ppsk[0].id };
        if (!msg.name) return this._toast("Give the device a name", true);
        if (f.network) msg.network = f.network;
        if (f.mac) msg.mac = f.mac.trim();
        if (Number(f.minutes)) msg.minutes = Number(f.minutes);
        else {
          if (Number(f.rotate_days)) msg.rotate_days = Number(f.rotate_days);
          if (Number(f.cutoff_days)) msg.cutoff_days = Number(f.cutoff_days);
        }
        if (f.key) {
          if (f.key.length < 8 || f.key.length > 63) return this._toast("A key needs 8 to 63 characters", true);
          msg.key = f.key;
        }
        this._setupWs(msg, "plan-key-add").then(async (result) => {
          if (!result) return;
          this._setupTake(result);
          const failed = (result.results || []).filter((r) => !r.ok);
          if (failed.length) this._toast(`Saved, but not yet on ${failed.map((r) => r.name).join(", ")}: ${failed[0].error}`, true);
          await this._planShowKey(result.key_id);
        });
        break;
      }
      case "plan-key-show":
        this._planShowKey(el.dataset.key);
        break;
      case "plan-key-revoke": {
        const key = s.state.plan.keys.find((k) => k.id === el.dataset.key);
        if (!key) return;
        this._openDialog({
          type: "confirm",
          title: `Revoke the key of ${key.name}?`,
          text: "The device is disconnected and can't join again with this key. Other devices aren't affected.",
          confirm: "Revoke",
          danger: true,
          onYes: () =>
            this._setupWs({ type: `${DOMAIN}/plan/key_revoke`, key_id: key.id }, `revoke:${key.id}`).then((r) => {
              if (!r) return;
              this._setupTake(r);
              this._toast(`Key of ${key.name} revoked`);
              this._render();
            }),
        });
        break;
      }
      case "plan-copy": {
        const value = d && d.value;
        const done = () => this._toast("Key copied");
        if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(value).then(done, () => this._toast("Select the key and copy it", true));
        else {
          const box = this.shadowRoot.getElementById("key-value");
          const range = document.createRange();
          range.selectNodeContents(box);
          const sel = this.shadowRoot.getSelection ? this.shadowRoot.getSelection() : window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          this._toast("Key selected: press Ctrl+C to copy");
        }
        break;
      }
      case "plan-wifi-qr": {
        const wifi = s.state.plan.wifi.find((w) => w.id === el.dataset.wifi);
        if (!wifi) return;
        if (s.dirty) return this._toast("Save the setup first", true);
        if (wifi.security === "open") {
          this._toast(`${wifi.ssid} is open: no password needed`);
          return;
        }
        this._setupWs({ type: `${DOMAIN}/plan/reveal`, ref: `wifi:${wifi.id}` }).then((r) => {
          if (!r) return;
          this._dialog = { type: "plan-key-show", name: wifi.ssid, ssid: wifi.ssid, value: r.value, qr: r.qr, wifiOnly: true, focus: '.dialog [data-action="close"]' };
          this._render();
        });
        break;
      }
      case "plan-key-rotate":
        if (s.dirty) return this._toast("Save the setup first", true);
        this._openDialog({ type: "plan-rotate", key: el.dataset.key });
        break;
      case "plan-key-rotate-start": {
        const f = d.form || {};
        const key = s.state.plan.keys.find((k) => k.id === d.key);
        const ids = f.all ? s.state.plan.keys.filter((k) => k.wifi === key.wifi && !k.rotation && !k.expires).map((k) => k.id) : [d.key];
        const msg = { type: `${DOMAIN}/plan/key_rotate`, key_ids: ids };
        const cutoff = Number(f.cutoff_days ?? key.cutoff_days ?? 0);
        if (cutoff) msg.cutoff_days = cutoff;
        this._setupWs(msg, "plan-key-rotate").then(async (r) => {
          if (!r) return;
          this._setupTake(r);
          this._toast(ids.length > 1 ? `New keys made for ${ids.length} devices` : `New key made for ${key.name}`);
          if (ids.length === 1) await this._planShowKey(d.key, true);
          else this._closeDialog();
        });
        break;
      }
      case "plan-key-show-next":
        this._planShowKey(el.dataset.key, true);
        break;
      case "plan-key-rotate-finish":
      case "plan-key-rotate-cancel": {
        const key = s.state.plan.keys.find((k) => k.id === el.dataset.key);
        const finish = action === "plan-key-rotate-finish";
        this._openDialog({
          type: "confirm",
          title: finish ? `Has ${key.name} got the new key?` : `Keep the old key of ${key.name}?`,
          text: finish ? "The new key becomes its key and the old one stops working." : "The new key is thrown away; the old one keeps working.",
          confirm: finish ? "Device updated" : "Keep old key",
          onYes: () =>
            this._setupWs({ type: `${DOMAIN}/plan/${finish ? "key_rotate_finish" : "key_rotate_cancel"}`, key_id: key.id }, `rot:${key.id}`).then((r) => {
              if (!r) return;
              this._setupTake(r);
              this._toast(finish ? `${key.name} now uses its new key` : `${key.name} keeps its old key`);
              this._render();
            }),
        });
        break;
      }
      case "plan-rules-edit":
        this._openDialog({ type: "plan-rules" });
        break;
      case "plan-rule-add": {
        const ob = plan.onboarding || {};
        const kinds = kindChoices(ob.kinds);
        const used = new Set(d.rules.map((r) => r.kind));
        const kind = (kinds.find(([id]) => !used.has(id)) || kinds[0])[0];
        const ppsk = plan.wifi.filter((w) => w.security === "ppsk");
        d.rules.push({ kind, wifi: ppsk[0].id, network: null, minutes: null, auto: false });
        this._render();
        break;
      }
      case "plan-rule-remove":
        d.rules.splice(Number(el.dataset.rule), 1);
        this._render();
        break;
      case "plan-rules-done": {
        const kinds = d.rules.map((r) => r.kind);
        if (new Set(kinds).size !== kinds.length) return this._toast("One rule per kind of device", true);
        plan.onboarding = { ...(plan.onboarding || {}), rules: d.rules };
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-portal-edit":
        this._openDialog({ type: "plan-portal", page: "form" });
        this._portalPreview();
        break;
      case "plan-portal-page":
        d.page = el.dataset.page;
        this._render();
        this._portalPreview();
        break;
      case "plan-portal-refresh":
        this._portalPreview();
        break;
      case "plan-portal-logo-remove":
        this._portalForm(d).logo = null;
        this._render();
        this._portalPreview();
        break;
      case "plan-portal-default":
        this._setupWs({ type: `${DOMAIN}/plan/portal_template` }).then((r) => {
          if (!r || this._dialog !== d) return;
          this._portalForm(d).custom_html = r.template;
          this._render();
          this._portalPreview();
        });
        break;
      case "plan-portal-done": {
        const f = this._portalForm(d);
        const settings = this._portalSettings(f);
        if (settings.use_custom && (settings.custom_html.match(/\{\{content\}\}/g) || []).length !== 1) return this._toast("Your own page needs {{content}} exactly once", true);
        if (settings.kinds.length > 12) return this._toast("Up to 12 kinds of device", true);
        plan.onboarding = { ...(plan.onboarding || {}), ...settings };
        this._dialog = null;
        this._setupDirty();
        break;
      }
      case "plan-ob-approve":
        if (s.dirty) return this._toast("Save the setup first", true);
        this._openDialog({ type: "plan-approve", request: el.dataset.request });
        break;
      case "plan-ob-approve-confirm": {
        const f = d.form || {};
        const req = (s.state.requests || []).find((r) => r.id === d.request);
        const offered = s.state.plan.wifi.filter((w) => w.security === "ppsk");
        const sug = (req && req.suggested) || {};
        const wifi = f.wifi || (offered.some((w) => w.id === sug.wifi) ? sug.wifi : req && offered.some((w) => w.id === req.wants) ? req.wants : offered[0] && offered[0].id);
        const msg = { type: `${DOMAIN}/plan/onboard_approve`, request_id: d.request, wifi, bind_mac: Boolean(f.bind) };
        if (f.network) msg.network = f.network;
        if (Number(f.minutes)) msg.minutes = Number(f.minutes);
        this._setupWs(msg, "plan-ob-approve").then((r) => {
          if (!r) return;
          this._setupTake(r);
          this._dialog = null;
          const failed = (r.results || []).filter((x) => !x.ok);
          if (failed.length) this._toast(`Approved, but the key isn't on ${failed.map((x) => x.name).join(", ")} yet: ${failed[0].error}`, true);
          else this._toast(`${req ? req.name : "Device"} approved: the portal now shows its password`);
          this._render();
        });
        break;
      }
      case "plan-ob-decline": {
        const req = (s.state.requests || []).find((r) => r.id === el.dataset.request);
        if (!req) return;
        this._openDialog({
          type: "confirm",
          title: `Decline ${req.name}?`,
          text: "The portal tells the device its request was declined. It can ask again in an hour.",
          confirm: "Decline",
          danger: true,
          onYes: () =>
            this._setupWs({ type: `${DOMAIN}/plan/onboard_decline`, request_id: req.id }, `decline:${req.id}`).then((r) => {
              if (!r) return;
              this._setupTake(r);
              this._toast(`${req.name} declined`);
              this._render();
            }),
        });
        break;
      }
      case "plan-review":
        (async () => {
          if (s.dirty && !(await this._planSave())) return;
          this._openDialog({ type: "plan-apply" });
          const preview = await this._setupWs({ type: `${DOMAIN}/plan/preview` });
          if (this._dialog && this._dialog.type === "plan-apply") {
            this._dialog.preview = preview || { errors: ["Couldn't work out the changes"], notes: [], devices: [] };
            this._render();
          }
        })();
        break;
      case "plan-apply":
        this._setupWs({ type: `${DOMAIN}/plan/apply` }, "plan-apply").then((r) => {
          if (!r) return;
          this._setupTake(r);
          if (this._dialog && this._dialog.type === "plan-apply") this._dialog.results = r.results;
          const failed = r.results.filter((x) => !x.ok).length;
          this._toast(failed ? `${failed} device${failed === 1 ? "" : "s"} failed` : "Setup applied", failed > 0);
          this._render();
        });
        break;
      default:
        break;
    }
  },

  async _planShowKey(keyId, upcoming = false) {
    const s = this._setupState();
    const key = s.state.plan.keys.find((k) => k.id === keyId);
    const r = await this._setupWs({ type: `${DOMAIN}/plan/reveal`, ref: upcoming ? `key:${keyId}:next` : `key:${keyId}` });
    if (!r || !key) return;
    const wifi = s.state.plan.wifi.find((w) => w.id === key.wifi);
    this._dialog = { type: "plan-key-show", name: upcoming ? `New key for ${key.name}` : key.name, device: key.name, upcoming, ssid: wifi && wifi.ssid, value: r.value, qr: r.qr, expires: key.expires, focus: '.dialog [data-action="close"]' };
    this._render();
  },
};

Object.assign(AerohiveNetworkPanel.prototype, SetupMixin);

/* Keyboard: Enter/Space on table rows opens the port. */
function rowKeyboard(root) {
  root.addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches && ev.target.matches("tr[tabindex]")) {
      ev.preventDefault();
      ev.target.click();
    }
  });
}

/* ------------------------------------------------------------------ */
/* Dashboard cards                                                      */
/* ------------------------------------------------------------------ */

const CARD_CSS = `
:host { display: block; }
ha-card, .ha-card { display: block; padding: 16px; overflow: hidden; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.head .avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 10px; background: color-mix(in srgb, var(--ah-accent) 14%, transparent); color: var(--ah-accent); flex: none; }
.head .avatar.off { background: color-mix(in srgb, var(--ah-text) 10%, transparent); color: var(--ah-muted); }
.name { font-size: 16px; font-weight: 500; }
.sub { font-size: 13px; color: var(--ah-muted); }
.fp-scroll { overflow-x: auto; padding: 8px 6px 4px; margin: -8px -6px 0; }
.foot { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 12px; font-size: 13px; color: var(--ah-muted); }
.foot b { color: var(--ah-text); font-weight: 500; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.dev-list { display: grid; gap: 2px; }
.dev { display: flex; align-items: center; gap: 12px; padding: 8px; margin: 0 -8px; border-radius: 10px; border: 0; background: transparent; text-align: left; cursor: pointer; width: calc(100% + 16px); }
.dev:hover { background: color-mix(in srgb, var(--ah-text) 6%, transparent); }
.dev .avatar { width: 32px; height: 32px; }
.totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(80px, 1fr)); gap: 8px; margin-bottom: 12px; }
.totals div { padding: 8px 10px; border-radius: 10px; background: color-mix(in srgb, var(--ah-text) 5%, transparent); }
.totals b { display: block; font-size: 20px; font-weight: 500; }
.totals span { font-size: 12px; color: var(--ah-muted); }
.totals .bad b { color: var(--ah-err); }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 12px 16px; }
.stat-label { font-size: 12px; color: var(--ah-muted); }
.stat-value { font-size: 17px; font-weight: 500; margin-bottom: 4px; }
.net-list { display: grid; gap: 10px; }
.net-list .net { padding: 12px; box-shadow: none; }
.net .avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 10px; flex: none;
  background: color-mix(in srgb, var(--ah-accent) 14%, transparent); color: var(--ah-accent); }
.net .avatar.off { background: color-mix(in srgb, var(--ah-text) 10%, transparent); color: var(--ah-muted); }
.tile-sub { font-size: 13px; color: var(--ah-muted); }
`;

class AerohiveCardBase extends AerohiveBase {
  constructor() {
    super();
    this.shadowRoot.innerHTML = `<style>${BASE_CSS}${CARD_CSS}</style><div id="card"></div><div id="dialog"></div>`;
    rowKeyboard(this.shadowRoot);
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  getCardSize() {
    return 4;
  }

  _wrap(inner) {
    const tag = customElements.get("ha-card") ? "ha-card" : "div";
    return `<${tag} class="ha-card ${tag === "div" ? "card" : ""}">${inner}</${tag}>`;
  }

  _device() {
    return findDevice(this._data, this._config && this._config.device);
  }

  _defaultDevice() {
    const device = this._device();
    return device ? device.entry_id : null;
  }

  _render() {
    const host = this.shadowRoot.getElementById("card");
    if (!host || !this._config) return;
    let inner;
    if (this._store && this._store.error) inner = `<div class="empty">Can't load network data: ${esc(this._store.error)}</div>`;
    else if (!this._data) inner = `<div class="empty">Loading…</div>`;
    else inner = this._cardHtml();
    host.innerHTML = this._wrap(inner);
    this._renderDialogInto(this.shadowRoot.getElementById("dialog"));
  }

  _openPanel(entryId) {
    const url = `/${PANEL_PATH}${entryId ? `/${entryId}` : ""}`;
    window.history.pushState(null, "", url);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  _onAction(action, el) {
    if (action === "open-panel") this._openPanel(el.dataset.open);
  }

  _missing() {
    return `<div class="empty">${this._config.device ? `Device “${esc(this._config.device)}” not found.` : "Choose a device in the card settings."}</div>`;
  }

  _head(device) {
    return `<div class="head" data-device="${esc(device.entry_id)}">
      <span class="avatar ${device.online ? "" : "off"}">${deviceIcon(device)}</span>
      <div class="grow"><div class="name ellipsis">${esc(this._config.title || device.name)}</div>
        <div class="sub ellipsis">${esc(deviceSummary(device))}</div></div>
      <button class="icon-btn" data-action="refresh" aria-label="Refresh now" title="Refresh now" ${this._isBusy(`refresh:${device.entry_id}`) ? "disabled" : ""}>${icon("reboot", this._isBusy(`refresh:${device.entry_id}`) ? "spin" : "")}</button>
      <button class="icon-btn" data-action="open-panel" data-open="${esc(device.entry_id)}" aria-label="Open in the Network panel" title="Open in the Network panel">${icon("back", "flip")}</button>
    </div>`;
  }
}

const FLIP_CSS = `.flip { transform: scaleX(-1); }`;

class AerohiveSwitchCard extends AerohiveCardBase {
  static getStubConfig(hass) {
    const store = hass ? stores.get(hass.connection) : null;
    const device = store && store.data ? store.data.devices.find((d) => d.kind === "switch") : null;
    return { device: device ? device.name : "" };
  }

  static getConfigElement() {
    return document.createElement("aerohive-card-editor");
  }

  getCardSize() {
    return 3;
  }

  /* Sections dashboards: a faceplate needs the full width. */
  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  _cardHtml() {
    const device = this._device();
    if (!device) return this._missing();
    const selected = this._dialog && this._dialog.type === "port" ? this._dialog.port : null;
    const cfg = device.config || {};
    const foot = [
      device.poe && device.poe.budget_w ? `PoE <b class="num">${esc(device.poe.used_w ?? 0)} / ${esc(device.poe.budget_w)} W</b>` : "",
      device.temperature != null ? `<b class="num">${esc(device.temperature)} °C</b>` : "",
      device.wired_clients ? `<b class="num">${esc(device.wired_clients)}</b> wired clients` : "",
      cfg.last_backup ? `Backup ${esc(fmtAgo(cfg.last_backup))}` : "",
    ].filter(Boolean);
    return `<style>${FLIP_CSS}</style>${this._head(device)}
      <div class="fp-scroll" data-device="${esc(device.entry_id)}">${renderFaceplate(device, { selected })}</div>
      ${this._config.legend ? renderLegend(device) : ""}
      ${foot.length ? `<div class="foot">${foot.map((f) => `<span>${f}</span>`).join("")}</div>` : ""}
      ${(device.problems || []).length || !device.online ? `<div class="chips">${problemChips(device)}</div>` : ""}`;
  }
}

class AerohiveDeviceCard extends AerohiveCardBase {
  static getStubConfig(hass) {
    const store = hass ? stores.get(hass.connection) : null;
    const device = store && store.data ? store.data.devices[0] : null;
    return { device: device ? device.name : "" };
  }

  static getConfigElement() {
    return document.createElement("aerohive-card-editor");
  }

  _cardHtml() {
    const device = this._device();
    if (!device) return this._missing();
    const cfg = device.config || {};
    const actions = this._config.actions !== false;
    return `<style>${FLIP_CSS}</style>${this._head(device)}
      ${(device.problems || []).length || !device.online ? `<div class="chips" style="margin:0 0 12px">${problemChips(device, 4)}</div>` : ""}
      ${healthTiles(device)}
      ${device.radios && device.radios.length ? `<div class="foot">${device.radios.map((r) => `<span><span class="dot ${r.up ? "ok" : "err"}" style="display:inline-block;margin-right:4px"></span>${esc(r.band || r.name)} ch <b class="num">${esc(r.channel ?? "–")}</b> · <b class="num">${esc(r.clients)}</b> clients</span>`).join("")}</div>` : ""}
      ${device.features && device.features.backup === false ? "" : `<div class="foot"><span>${cfg.last_backup ? `Last backup <b>${esc(fmtAgo(cfg.last_backup))}</b>` : "No backup yet"}</span></div>`}
      ${
        actions
          ? `<div class="actions" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px" data-device="${esc(device.entry_id)}">
              ${cfg.unsaved ? `<button class="btn attention" data-action="save" ${this._isBusy(`save:${device.entry_id}`) ? "disabled" : ""}>${icon("save")}Save configuration</button>` : ""}
              ${cfg.changed_after_reboot && !(device.features && device.features.restore === false) ? `<button class="btn primary" data-action="restore">${icon("restore")}Restore last backup</button>` : ""}
              <button class="btn danger" data-action="reboot" ${device.online ? "" : "disabled"}>${icon("reboot")}Reboot</button>
            </div>`
          : ""
      }`;
  }
}

class AerohiveNetworkCard extends AerohiveCardBase {
  static getStubConfig() {
    return {};
  }

  _cardHtml() {
    const devices = this._data.devices || [];
    const online = devices.filter((d) => d.online).length;
    const wireless = devices.reduce((n, d) => n + (d.stations || []).length, 0);
    const wired = devices.reduce((n, d) => n + (d.wired_clients || 0), 0);
    const problems = devices.reduce((n, d) => n + (d.online ? (d.problems || []).length : 1), 0);
    return `<style>${FLIP_CSS}</style>
      <div class="head"><span class="avatar">${icon("switch")}</span><div class="grow"><div class="name">${esc(this._config.title || "Network")}</div>
        <div class="sub">${online} of ${devices.length} devices online</div></div>
        <button class="icon-btn" data-action="refresh" aria-label="Refresh all devices now" title="Refresh all devices now" ${this._isBusy("refresh:all") ? "disabled" : ""}>${icon("reboot", this._isBusy("refresh:all") ? "spin" : "")}</button>
        <button class="icon-btn" data-action="open-panel" data-open="" aria-label="Open the Network panel" title="Open the Network panel">${icon("back", "flip")}</button></div>
      <div class="totals">
        <div><b class="num">${wireless}</b><span>Wi-Fi clients</span></div>
        <div><b class="num">${wired}</b><span>Wired clients</span></div>
        <div class="${problems ? "bad" : ""}"><b class="num">${problems}</b><span>Problems</span></div>
      </div>
      <div class="dev-list">${devices
        .map(
          (d) => `<button class="dev" data-action="open-panel" data-open="${esc(d.entry_id)}">
            <span class="avatar ${d.online ? "" : "off"}" style="display:grid;place-items:center;border-radius:9px;background:color-mix(in srgb, var(--ah-accent) 12%, transparent);color:var(--ah-accent)">${deviceIcon(d)}</span>
            <span class="grow"><span class="name ellipsis" style="display:block;font-size:14px">${esc(d.name)}</span><span class="sub ellipsis" style="display:block">${esc(deviceSummary(d))}</span></span>
            <span class="dot ${d.online ? ((d.problems || []).length ? "warn" : "ok") : "err"}"></span>
          </button>`,
        )
        .join("")}</div>`;
  }
}

class AerohiveWifiCard extends AerohiveCardBase {
  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 3;
  }

  _cardHtml() {
    const { aps, networks } = wifiNetworks(this._data);
    const wanted = Array.isArray(this._config.networks) && this._config.networks.length ? this._config.networks : null;
    const shown = wanted ? networks.filter((n) => wanted.includes(n.name)) : networks;
    const admin = this._isAdmin();
    return `<style>${FLIP_CSS}</style>
      <div class="head"><span class="avatar">${icon("wifi")}</span><div class="grow"><div class="name">${esc(this._config.title || "Wi-Fi")}</div>
        <div class="sub">${shown.length} network${shown.length === 1 ? "" : "s"} · ${aps.length} access point${aps.length === 1 ? "" : "s"}</div></div>
        ${admin && !wanted ? `<button class="icon-btn" data-action="wifi-add" aria-label="Add a Wi-Fi network" title="Add a Wi-Fi network">${icon("plus")}</button>` : ""}
        <button class="icon-btn" data-action="open-wifi" aria-label="Open Wi-Fi in the Network panel" title="Open Wi-Fi in the Network panel">${icon("back", "flip")}</button></div>
      ${shown.length ? `<div class="net-list">${shown.map((n) => this._networkCard(n, { compact: true })).join("")}</div>` : `<div class="empty">${aps.length ? "No matching Wi-Fi networks." : "No access points online."}</div>`}`;
  }

  _onAction(action, el) {
    if (action === "open-wifi") this._openPanel("wifi");
    else super._onAction(action, el);
  }
}

/* Card editor: pick the device (and a title). */
class AerohiveCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._unsub) {
      this._unsub = getStore(hass).subscribe((data) => {
        this._data = data;
        this._render();
      });
    }
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
  }

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("change", (ev) => {
        const key = ev.target.dataset.key;
        if (!key) return;
        const value = ev.target.type === "checkbox" ? ev.target.checked : ev.target.value;
        this._config = { ...this._config, [key]: value };
        if (value === "" || value === undefined) delete this._config[key];
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
      });
    }
    const devices = (this._data && this._data.devices) || [];
    this.shadowRoot.innerHTML = `<style>${BASE_CSS}
      label { display: grid; gap: 4px; margin-bottom: 12px; font-size: 13px; color: var(--ah-muted); }
      select, input[type=text] { height: 40px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--ah-line); background: var(--ah-card); color: var(--ah-text); font: inherit; }
      .check { display: flex; align-items: center; gap: 8px; }</style>
      <label>Device<select data-key="device">
        <option value="">Choose…</option>
        ${devices.map((d) => `<option value="${esc(d.name)}" ${this._config.device === d.name ? "selected" : ""}>${esc(d.name)} (${esc(d.model || d.kind)})</option>`).join("")}
      </select></label>
      <label>Title (optional)<input type="text" data-key="title" value="${esc(this._config.title || "")}"></label>
      <label class="check"><input type="checkbox" data-key="legend" ${this._config.legend ? "checked" : ""}>Show colour legend (switch card)</label>`;
  }
}

/* ------------------------------------------------------------------ */
/* Registration                                                         */
/* ------------------------------------------------------------------ */

const ELEMENTS = {
  "aerohive-network-panel": AerohiveNetworkPanel,
  "aerohive-switch-card": AerohiveSwitchCard,
  "aerohive-device-card": AerohiveDeviceCard,
  "aerohive-network-card": AerohiveNetworkCard,
  "aerohive-wifi-card": AerohiveWifiCard,
  "aerohive-card-editor": AerohiveCardEditor,
};
for (const [tag, cls] of Object.entries(ELEMENTS)) {
  if (!customElements.get(tag)) customElements.define(tag, cls);
}

window.customCards = window.customCards || [];
for (const card of [
  { type: "aerohive-switch-card", name: "Aerohive switch", description: "Switch faceplate with live port status, PoE and port controls." },
  { type: "aerohive-device-card", name: "Aerohive device", description: "Health, configuration status and actions for one access point, router or switch." },
  { type: "aerohive-network-card", name: "Aerohive network", description: "Every Aerohive / Extreme Networks device at a glance." },
  { type: "aerohive-wifi-card", name: "Aerohive Wi-Fi", description: "Turn Wi-Fi networks on or off, also for a while (e.g. guest Wi-Fi for 2 hours)." },
]) {
  if (!window.customCards.some((c) => c.type === card.type)) window.customCards.push({ ...card, preview: true, documentationURL: "https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/blob/main/docs/dashboard.md" });
}
