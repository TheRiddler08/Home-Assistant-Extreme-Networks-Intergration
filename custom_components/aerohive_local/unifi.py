"""Ubiquiti UniFi access points and switches, through the UniFi Network controller's local API.

UniFi devices are configured by their controller (a UniFi OS console such as a Cloud Key,
UDM/UDR/UCG or UniFi OS Server, or the self-hosted Network application), so Home Assistant
talks to the controller, not to the devices. One config entry per device; entries of the
same controller share one logged-in session and one copy of the device and client lists.

API (the controller's own web API, used by its web page and widely documented):

- UniFi OS: ``POST /api/auth/login`` (cookie + ``X-CSRF-Token``), then everything under
  ``/proxy/network``. The self-hosted application: ``POST /api/login``, no prefix.
- ``GET  api/s/<site>/stat/device``          devices: ports, radios, Wi-Fi networks, stats
- ``GET  api/s/<site>/stat/sta``             connected clients
- ``GET  api/s/<site>/rest/wlanconf``        Wi-Fi networks (``PUT rest/wlanconf/<id>`` changes one)
- ``PUT  api/s/<site>/rest/device/<id>``     ``port_overrides`` (PoE mode, port on/off)
- ``POST api/s/<site>/cmd/devmgr``           ``power-cycle`` a PoE port, ``restart`` a device
- ``POST api/s/<site>/cmd/stamgr``           ``kick-sta`` (disconnect a client)

Parsing is pure (tested with responses of the documented shape); the client does the HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .client import AerohiveAuthError, AerohiveConnectionError

_LOGGER = logging.getLogger(__name__)

DIALECT_UNIFI = "unifi"
DEFAULT_PORT = 443
DEFAULT_SITE = "default"
CACHE_SECONDS = 4  # device entries of one controller poll at about the same time: one read serves all
RADIO_BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}
TYPE_KINDS = {
    "uap": "ap",
    "usw": "switch",
    "ugw": "router",
    "udm": "router",
    "uxg": "router",
}


class UniFiError(AerohiveConnectionError):
    pass


class UniFiAuthError(AerohiveAuthError, UniFiError):
    pass


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mac(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 17 else None


@dataclass
class Port:
    index: int
    name: str | None
    up: bool
    enabled: bool
    speed: int | None  # Mbit/s
    full_duplex: bool | None
    uplink: bool
    poe_capable: bool
    poe_on: bool | None
    poe_power: float | None
    poe_class: str | None
    rx_bytes: int | None
    tx_bytes: int | None
    rx_errors: int | None
    tx_errors: int | None
    media: str | None  # "GE", "SFP+", ...


@dataclass
class Radio:
    name: str  # wifi0
    band: str | None  # 2.4 GHz
    channel: int | None
    width: int | None
    tx_power: float | None
    clients: int | None


@dataclass
class Vap:
    essid: str
    radio: str  # wifi0
    up: bool
    clients: int | None


@dataclass
class Device:
    id: str
    mac: str
    name: str
    kind: str  # ap / switch / router
    type: str  # uap / usw / ...
    model: str | None
    version: str | None
    ip: str | None
    connected: bool
    uptime: int | None
    cpu: float | None
    memory: float | None  # % used
    temperature: float | None
    ports: list[Port] = field(default_factory=list)
    radios: list[Radio] = field(default_factory=list)
    vaps: list[Vap] = field(default_factory=list)
    poe_budget: float | None = None
    port_overrides: list[dict[str, Any]] = field(default_factory=list)


def parse_devices(payload: Any) -> dict[str, Device]:
    """``stat/device`` -> devices by MAC."""
    result: dict[str, Device] = {}
    for raw in payload or []:
        if not isinstance(raw, dict) or not _mac(raw.get("mac")):
            continue
        mac = _mac(raw.get("mac"))
        stats = raw.get("system-stats") or {}
        temps = [
            _float(t.get("value"))
            for t in raw.get("temperatures") or []
            if isinstance(t, dict)
        ]
        temps = [t for t in temps if t is not None]
        temperature = (
            _float(raw.get("general_temperature"))
            if raw.get("has_temperature")
            else (max(temps) if temps else None)
        )
        device = Device(
            id=str(raw.get("_id") or ""),
            mac=mac,
            name=str(raw.get("name") or raw.get("model") or mac),
            kind=TYPE_KINDS.get(str(raw.get("type")), "unknown"),
            type=str(raw.get("type") or ""),
            model=raw.get("model"),
            version=raw.get("version"),
            ip=raw.get("ip"),
            connected=_int(raw.get("state")) == 1,
            uptime=_int(raw.get("uptime")),
            cpu=_float(stats.get("cpu")),
            memory=_float(stats.get("mem")),
            temperature=temperature,
            poe_budget=_float(raw.get("total_max_power")),
            port_overrides=[
                o for o in raw.get("port_overrides") or [] if isinstance(o, dict)
            ],
        )
        for port in raw.get("port_table") or []:
            if not isinstance(port, dict) or _int(port.get("port_idx")) is None:
                continue
            poe = bool(port.get("port_poe"))
            device.ports.append(
                Port(
                    index=_int(port["port_idx"]),  # type: ignore[arg-type]
                    name=port.get("name"),
                    up=bool(port.get("up")),
                    enabled=port.get("enable", True) is not False,
                    speed=_int(port.get("speed")) or None,
                    full_duplex=port.get("full_duplex"),
                    uplink=bool(port.get("is_uplink")),
                    poe_capable=poe,
                    poe_on=(str(port.get("poe_mode") or "auto") != "off")
                    if poe
                    else None,
                    poe_power=_float(port.get("poe_power")) if poe else None,
                    poe_class=port.get("poe_class") if poe else None,
                    rx_bytes=_int(port.get("rx_bytes")),
                    tx_bytes=_int(port.get("tx_bytes")),
                    rx_errors=_int(port.get("rx_errors")),
                    tx_errors=_int(port.get("tx_errors")),
                    media=port.get("media"),
                )
            )
        stats_by_name = {
            s.get("name"): s
            for s in raw.get("radio_table_stats") or []
            if isinstance(s, dict)
        }
        for radio in raw.get("radio_table") or []:
            if not isinstance(radio, dict) or not radio.get("name"):
                continue
            stat = stats_by_name.get(radio["name"], {})
            device.radios.append(
                Radio(
                    name=str(radio["name"]),
                    band=RADIO_BANDS.get(str(radio.get("radio"))),
                    channel=_int(stat.get("channel") or radio.get("channel")),
                    width=_int(radio.get("ht")),
                    tx_power=_float(stat.get("tx_power")),
                    clients=_int(stat.get("num_sta")),
                )
            )
        for vap in raw.get("vap_table") or []:
            if isinstance(vap, dict) and vap.get("essid") and vap.get("radio_name"):
                device.vaps.append(
                    Vap(
                        essid=str(vap["essid"]),
                        radio=str(vap["radio_name"]),
                        up=vap.get("up", True) is not False,
                        clients=_int(vap.get("num_sta")),
                    )
                )
        result[mac] = device
    return result


@dataclass
class Client:
    mac: str
    ip: str | None
    hostname: str | None
    name: str | None
    wired: bool
    ap_mac: str | None
    essid: str | None
    radio: str | None  # ng / na / 6e
    channel: int | None
    signal: int | None
    tx_rate: int | None  # kbit/s
    rx_rate: int | None
    uptime: int | None
    vlan: int | None
    sw_mac: str | None
    sw_port: int | None


def parse_clients(payload: Any) -> list[Client]:
    result = []
    for raw in payload or []:
        if not isinstance(raw, dict) or not _mac(raw.get("mac")):
            continue
        result.append(
            Client(
                mac=_mac(raw.get("mac")),  # type: ignore[arg-type]
                ip=raw.get("ip") or raw.get("last_ip"),
                hostname=raw.get("hostname"),
                name=raw.get("name"),
                wired=bool(raw.get("is_wired")),
                ap_mac=_mac(raw.get("ap_mac")),
                essid=raw.get("essid"),
                radio=raw.get("radio"),
                channel=_int(raw.get("channel")),
                signal=_int(
                    raw.get("signal")
                    if raw.get("signal") is not None
                    else raw.get("rssi")
                ),
                tx_rate=_int(raw.get("tx_rate")),
                rx_rate=_int(raw.get("rx_rate")),
                uptime=_int(raw.get("uptime")),
                vlan=_int(raw.get("vlan")),
                sw_mac=_mac(raw.get("sw_mac")),
                sw_port=_int(raw.get("sw_port")),
            )
        )
    return result


@dataclass
class Wlan:
    id: str
    name: str
    enabled: bool
    hidden: bool
    security: str  # open / wpapsk / wpaeap / ...
    ppsk: bool
    bands: list[str]


def parse_wlans(payload: Any) -> dict[str, Wlan]:
    result = {}
    for raw in payload or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        band = str(raw.get("wlan_band") or "both")
        bands = list(raw.get("wlan_bands") or ([] if band == "both" else [band])) or [
            "2g",
            "5g",
        ]
        result[str(raw["name"])] = Wlan(
            id=str(raw.get("_id") or ""),
            name=str(raw["name"]),
            enabled=raw.get("enabled", True) is not False,
            hidden=bool(raw.get("hide_ssid")),
            security=str(raw.get("security") or "open"),
            ppsk=bool(raw.get("private_preshared_keys_enabled")),
            bands=bands,
        )
    return result


def merged_overrides(
    overrides: list[dict[str, Any]], index: int, changes: dict[str, Any]
) -> list[dict[str, Any]]:
    """``port_overrides`` with one port's settings changed (the controller replaces the whole list)."""
    result, found = [], False
    for item in overrides:
        if _int(item.get("port_idx")) == index:
            item = {**item, **changes}
            found = True
        result.append(item)
    if not found:
        result.append({"port_idx": index, **changes})
    return result


def rate_text(kbps: int | None) -> str | None:
    if not kbps:
        return None
    return f"{kbps / 1000:.0f}M" if kbps >= 1000 else f"{kbps}K"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class UniFiController:
    """One logged-in session to a controller site, shared by that controller's device entries."""

    def __init__(
        self,
        session: Any,
        host: str,
        port: int,
        username: str,
        password: str,
        site: str,
        *,
        verify_ssl: bool,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.site = site or DEFAULT_SITE
        self._username = username
        self._password = password
        self._ssl = None if verify_ssl else False
        self._root = f"https://{host}" + (f":{port}" if port != 443 else "")
        self._prefix: str | None = (
            None  # "/proxy/network" on UniFi OS, "" on the self-hosted application
        )
        self._csrf: str | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _raw(
        self, method: str, url: str, payload: Any = None, *, timeout: float = 20
    ) -> tuple[int, Any, Any]:
        import aiohttp

        headers = {"X-CSRF-Token": self._csrf} if self._csrf and method != "GET" else {}
        try:
            async with self._session.request(
                method,
                url,
                json=payload,
                ssl=self._ssl,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if token := response.headers.get(
                    "X-CSRF-Token"
                ) or response.headers.get("x-updated-csrf-token"):
                    self._csrf = token
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, body, response.headers
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UniFiError(
                f"Cannot reach the UniFi controller at {self.host}: {err}"
            ) from err

    async def login(self) -> None:
        credentials = {
            "username": self._username,
            "password": self._password,
            "remember": True,
            "rememberMe": True,
        }
        status, _, _ = await self._raw(
            "POST", f"{self._root}/api/auth/login", credentials
        )
        if status == 200:
            self._prefix = "/proxy/network"
            return
        if status in (401, 403):
            raise UniFiAuthError(
                f"The UniFi controller at {self.host} refused the username or password"
            )
        status, body, _ = await self._raw(
            "POST", f"{self._root}/api/login", credentials
        )
        if status == 200:
            self._prefix = ""
            return
        if status in (400, 401, 403) or (
            isinstance(body, dict)
            and (body.get("meta") or {}).get("msg") == "api.err.Invalid"
        ):
            raise UniFiAuthError(
                f"The UniFi controller at {self.host} refused the username or password"
            )
        raise UniFiError(
            f"{self.host} doesn't answer like a UniFi Network controller (HTTP {status})"
        )

    async def request(self, method: str, path: str, payload: Any = None) -> Any:
        """``path`` below ``api/s/<site>/``, e.g. ``stat/device``. Returns the ``data`` list."""
        for attempt in (1, 2):
            if self._prefix is None:
                await self.login()
            url = f"{self._root}{self._prefix}/api/s/{self.site}/{path.lstrip('/')}"
            status, body, _ = await self._raw(method, url, payload)
            if status == 401 and attempt == 1:
                self._prefix, self._csrf = None, None  # session expired: log in again
                continue
            if status in (401, 403):
                raise UniFiAuthError(
                    f"The UniFi controller at {self.host} refused the request (HTTP {status})"
                )
            meta = (body or {}).get("meta") if isinstance(body, dict) else None
            if status >= 400 or (meta and meta.get("rc") != "ok"):
                message = (meta or {}).get("msg") or f"HTTP {status}"
                raise UniFiError(f"The UniFi controller refused {path}: {message}")
            return (body or {}).get("data") if isinstance(body, dict) else body
        raise UniFiError(
            f"The UniFi controller at {self.host} keeps ending the session"
        )

    async def cached(self, path: str) -> Any:
        async with self._lock:
            hit = self._cache.get(path)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return hit[1]
            data = await self.request("GET", path)
            self._cache[path] = (time.monotonic(), data)
            return data

    def forget(self) -> None:
        self._cache.clear()

    async def devices(self) -> dict[str, Device]:
        return parse_devices(await self.cached("stat/device"))

    async def clients(self) -> list[Client]:
        return parse_clients(await self.cached("stat/sta"))

    async def wlans(self) -> dict[str, Wlan]:
        return parse_wlans(await self.cached("rest/wlanconf"))

    async def command(self, manager: str, payload: dict[str, Any]) -> None:
        await self.request("POST", f"cmd/{manager}", payload)
        self.forget()

    async def update(self, path: str, payload: dict[str, Any]) -> None:
        await self.request("PUT", path, payload)
        self.forget()


class UniFiClient:
    """One UniFi device as the rest of the integration sees a device client (no CLI)."""

    dialect = DIALECT_UNIFI
    config_unsaved = None

    def __init__(
        self, controller: UniFiController, mac: str, host: str | None = None
    ) -> None:
        self.controller = controller
        self.mac = mac.lower()
        self.host = host or controller.host
        self.hostname: str | None = None
        self.connected = True

    async def connect(self) -> None:
        return None

    async def run(self, command: str, **kwargs: Any) -> str:
        raise UniFiError(
            "UniFi devices are managed through their controller; CLI commands aren't available"
        )

    async def run_sequence(self, commands: Any, **kwargs: Any) -> list[str]:
        raise UniFiError(
            "UniFi devices are managed through their controller; CLI commands aren't available"
        )

    async def async_set_dialect(self, dialect: str) -> None:
        return None

    async def reboot(self, command: str = "", *, save: bool = False) -> None:
        await self.controller.command("devmgr", {"cmd": "restart", "mac": self.mac})

    async def close(self) -> None:
        return None


_CONTROLLERS: dict[tuple[str, int, str, str], UniFiController] = {}


def shared_controller(
    session: Any,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    *,
    verify_ssl: bool,
) -> UniFiController:
    """The controller session for these settings (one per controller and site, shared by its devices)."""
    key = (host, port, username, site or DEFAULT_SITE)
    controller = _CONTROLLERS.get(key)
    if controller is None or controller._password != password:
        controller = _CONTROLLERS[key] = UniFiController(
            session, host, port, username, password, site, verify_ssl=verify_ssl
        )
    return controller
