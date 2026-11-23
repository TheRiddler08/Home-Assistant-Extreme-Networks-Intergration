"""OPNsense routers, through OPNsense's REST API (API key and secret).

Unlike the other devices, OPNsense isn't driven over SSH: everything is read
with HTTPS requests to ``/api/...`` (basic authentication with the key and
secret of an OPNsense user, created under System > Access > Users > API keys).
The endpoints and response shapes below follow the API controllers in
OPNsense's source (opnsense/core, src/opnsense/mvc/app/controllers):

- ``diagnostics/system/system_information``: hostname and versions
- ``diagnostics/system/system_resources``: memory
- ``diagnostics/system/system_temperature``: sensors
- ``diagnostics/activity/get_activity``: top's header (CPU idle, uptime)
- ``interfaces/overview/interfaces_info/1``: interfaces with status, MAC,
  media, addresses, gateways and counters
- ``routes/gateway/status``: gateway health (loss, delay)
- ``diagnostics/interface/get_arp``: neighbours (IP, MAC, hostname)
- ``kea/leases4/search``, ``dnsmasq/leases/search``,
  ``dhcpv4/leases/search_lease``: DHCP leases (whichever server is in use)
- ``core/system/reboot`` (POST)

The parsers are pure Python and accept missing fields: anything not
reported is just unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .client import AerohiveAuthError, AerohiveConnectionError

DIALECT_OPNSENSE = "opnsense"
DEFAULT_PORT = 443

SYSTEM_INFORMATION = "diagnostics/system/system_information"
SYSTEM_RESOURCES = "diagnostics/system/system_resources"
SYSTEM_TEMPERATURE = "diagnostics/system/system_temperature"
ACTIVITY = "diagnostics/activity/get_activity"
INTERFACES = "interfaces/overview/interfaces_info/1"
GATEWAYS = "routes/gateway/status"
ARP = "diagnostics/interface/get_arp"
LEASES = ("kea/leases4/search", "dnsmasq/leases/search", "dhcpv4/leases/search_lease")
REBOOT = "core/system/reboot"

_PHYSICAL_RE = re.compile(r"^(igc|igb|em|ix|ixl|ice|re|bge|bnxt|vtnet|mlxen|mce|cxl|ql|axe|axge|ue|hn|vmx|lagg)\d+$")
_MAC_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class SystemInfo:
    hostname: str | None = None
    version: str | None = None  # "OPNsense 25.7.3-amd64"
    firmware: str | None = None  # "25.7.3"


@dataclass
class Port:
    device: str  # igc0
    label: str | None = None  # WAN, LAN, the interface's description
    identifier: str | None = None  # wan, lan, opt1
    up: bool | None = None
    enabled: bool | None = None
    mac: str | None = None
    speed: int | None = None  # Mbit/s
    duplex: str | None = None
    ipv4: str | None = None  # "192.168.1.1/24"
    wan: bool = False
    vlan: int | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    physical: bool = True


@dataclass
class Gateway:
    name: str
    address: str | None = None
    status: str | None = None  # none (= online), down, loss, delay, ...
    online: bool | None = None
    loss: str | None = None
    delay: str | None = None
    interface: str | None = None


@dataclass
class Neighbor:
    mac: str
    ip: str | None = None
    hostname: str | None = None
    interface: str | None = None  # device, e.g. igc1


@dataclass
class Activity:
    cpu_percent: float | None = None
    uptime_seconds: int | None = None
    load: list[float] = field(default_factory=list)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Search endpoints answer {"rows": [...]}; some answer a plain list or a dict of items."""
    if isinstance(payload, dict):
        if isinstance(payload.get("rows"), list):
            return [r for r in payload["rows"] if isinstance(r, dict)]
        if isinstance(payload.get("items"), list):
            return [r for r in payload["items"] if isinstance(r, dict)]
        return [dict(v, _key=k) for k, v in payload.items() if isinstance(v, dict)]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _mac(value: Any) -> str | None:
    match = _MAC_RE.search(str(value or ""))
    return match.group(1).lower() if match else None


def parse_system_information(payload: Any) -> SystemInfo:
    info = SystemInfo()
    if not isinstance(payload, dict):
        return info
    info.hostname = (str(payload.get("name") or "").split(".")[0]) or None
    versions = payload.get("versions") or []
    if versions and isinstance(versions, list):
        info.version = str(versions[0])
        if match := re.search(r"(\d+\.\d+(?:\.\d+)?(?:_\d+)?)", info.version):
            info.firmware = match.group(1)
    return info


def parse_memory(payload: Any) -> tuple[int | None, int | None]:
    """(total, used) in kB from system_resources."""
    memory = payload.get("memory") if isinstance(payload, dict) else None
    if not isinstance(memory, dict):
        return None, None
    total, used = _int(memory.get("total")), _int(memory.get("used"))
    return (total // 1024 if total else None), (used // 1024 if used is not None else None)


def parse_temperature(payload: Any) -> float | None:
    """The hottest CPU (else any) sensor, in °C."""
    best: float | None = None
    rows = _rows(payload)
    cpu = [r for r in rows if r.get("type") == "cpu"] or rows
    for row in cpu:
        try:
            value = float(str(row.get("temperature")).strip())
        except (TypeError, ValueError):
            continue
        best = value if best is None else max(best, value)
    return best


_IDLE_RE = re.compile(r"([\d.]+)%\s*idle", re.IGNORECASE)
_UP_RE = re.compile(r"\bup\s+(?:(\d+)\+)?(\d+):(\d+):(\d+)")
_LOAD_RE = re.compile(r"load averages?:\s*([\d.]+),?\s+([\d.]+),?\s+([\d.]+)")


def parse_activity(payload: Any) -> Activity:
    """top's header lines: 'CPU: ... 96.8% idle', '... up 3+04:12:33 ...', 'load averages: ...'."""
    activity = Activity()
    headers = payload.get("headers") if isinstance(payload, dict) else None
    for line in headers if isinstance(headers, list) else []:
        text = str(line)
        if activity.cpu_percent is None and (m := _IDLE_RE.search(text)) and "cpu" in text.lower():
            activity.cpu_percent = round(max(0.0, 100.0 - float(m.group(1))), 1)
        if activity.uptime_seconds is None and (m := _UP_RE.search(text)):
            days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
            activity.uptime_seconds = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
        if not activity.load and (m := _LOAD_RE.search(text)):
            activity.load = [float(x) for x in m.groups()]
    return activity


_SPEED_RE = re.compile(r"(\d+)(G|M)?base", re.IGNORECASE)


def parse_media(media: Any) -> tuple[int | None, str | None]:
    """'Ethernet autoselect (1000baseT <full-duplex>)' -> (1000, 'full')."""
    text = str(media or "")
    speed = None
    if match := _SPEED_RE.search(text):
        number = int(match.group(1))
        speed = number * 1000 if (match.group(2) or "").upper() == "G" else number
    duplex = "full" if "full-duplex" in text else "half" if "half-duplex" in text else None
    return speed, duplex


def _stat(stats: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if name in stats:
            return _int(stats[name])
    return None


def parse_interfaces(payload: Any) -> dict[str, Port]:
    """Interfaces overview -> physical ports by device name (igc0...), with their assignment if any."""
    ports: dict[str, Port] = {}
    for row in _rows(payload):
        device = str(row.get("device") or row.get("_key") or "").strip()
        if not device:
            continue
        physical = row.get("is_physical")
        physical = bool(physical) if physical is not None else bool(_PHYSICAL_RE.match(device))
        if not physical or "." in device or device.startswith(("lo", "pflog", "pfsync", "enc", "ovpn", "wg", "tun", "gif")):
            continue
        speed, duplex = parse_media(row.get("media"))
        status = str(row.get("status") or "").lower()
        stats = row.get("statistics") if isinstance(row.get("statistics"), dict) else {}
        identifier = str(row.get("identifier") or "") or None
        gateways = row.get("gateways") or []
        description = str(row.get("description") or "") or None
        ports[device] = Port(
            device=device,
            label=description if identifier else None,
            identifier=identifier,
            up=status in ("up", "active") if status else None,
            enabled=row.get("enabled") if isinstance(row.get("enabled"), bool) else None,
            mac=_mac(row.get("macaddr")),
            speed=speed,
            duplex=duplex,
            ipv4=str(row.get("addr4") or "") or None,
            wan=identifier == "wan" or bool(gateways),
            vlan=_int(row.get("vlan_tag")),
            rx_bytes=_stat(stats, "bytes received", "bytes_received", "ibytes"),
            tx_bytes=_stat(stats, "bytes transmitted", "bytes_transmitted", "obytes"),
            rx_errors=_stat(stats, "input errors", "input_errors", "ierrors"),
            tx_errors=_stat(stats, "output errors", "output_errors", "oerrors"),
        )
    return ports


def assigned_networks(payload: Any) -> list[dict[str, Any]]:
    """Assigned interfaces with an address (including VLANs): name, device, address, VLAN tag."""
    result = []
    for row in _rows(payload):
        if row.get("identifier") and row.get("addr4"):
            result.append(
                {
                    "name": str(row.get("description") or row.get("identifier")),
                    "device": str(row.get("device") or ""),
                    "address": str(row["addr4"]),
                    "vlan": _int(row.get("vlan_tag")),
                }
            )
    return result


def parse_gateways(payload: Any) -> list[Gateway]:
    result = []
    for row in _rows(payload):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        status = str(row.get("status") or "").lower() or None
        result.append(
            Gateway(
                name=name,
                address=str(row.get("address") or "") or None,
                status=status,
                online=None if status is None else status in ("none", "online", "up"),
                loss=str(row.get("loss") or "") or None,
                delay=str(row.get("delay") or "") or None,
                interface=str(row.get("interface") or row.get("if") or "") or None,
            )
        )
    return result


def parse_arp(payload: Any) -> list[Neighbor]:
    result = []
    for row in _rows(payload):
        mac = _mac(row.get("mac"))
        if not mac or mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or row.get("permanent") is True:
            continue
        hostname = str(row.get("hostname") or "").strip() or None
        result.append(Neighbor(mac=mac, ip=str(row.get("ip") or "") or None, hostname=hostname, interface=str(row.get("intf") or "") or None))
    return result


def parse_leases(payload: Any) -> dict[str, tuple[str | None, str | None]]:
    """DHCP leases (Kea, Dnsmasq or ISC): MAC -> (IP, hostname)."""
    result: dict[str, tuple[str | None, str | None]] = {}
    for row in _rows(payload):
        mac = _mac(row.get("hwaddr") or row.get("mac"))
        if not mac:
            continue
        ip = str(row.get("address") or row.get("ip") or "") or None
        hostname = str(row.get("hostname") or row.get("client-hostname") or "").strip().rstrip(".") or None
        result[mac] = (ip, hostname if hostname and hostname != "*" else None)
    return result


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


_MEDIA_RE = re.compile(r"^(\d+(?:\.\d+)?)(G)?base", re.IGNORECASE)


def parse_media_options(payload: Any) -> tuple[str, dict[tuple[str, str], str]]:
    """An interface's media choices from ``interfaces/assignment/get_item/<if>``.

    Returns (selected key or "" for automatic, {(speed Mbit/s, duplex): key}).
    Keys look like "1000baseT\tfull-duplex" or "2500Base-T".
    """
    media = ((payload or {}).get("interface") or {}).get("media") if isinstance(payload, dict) else None
    if not isinstance(media, dict):
        return "", {}
    current, choices = "", {}
    for key, value in media.items():
        if isinstance(value, dict) and str(value.get("selected")) in ("1", "True", "true"):
            current = key
        match = _MEDIA_RE.match(key or "")
        if not match:
            continue
        speed = float(match.group(1)) * (1000 if match.group(2) else 1)
        duplex = "half" if "half-duplex" in key else "full"
        choices.setdefault((str(int(speed)), duplex), key)
    return current, choices


def media_setting(current: str, choices: dict[tuple[str, str], str]) -> tuple[str, str]:
    for setting, key in choices.items():
        if key == current:
            return setting
    return ("auto", "auto")


def assignment_path(identifier: str) -> str:
    return f"interfaces/assignment/set_item/{identifier}"


CONFIG_DOWNLOAD = "core/backup/download/this"
CONFIG_COMMAND = "opnsense:config.xml"  # what the backup code "runs" to read the configuration
_SECRET_TAGS = (
    "password", "passwd", "bcrypt-hash", "sha512-hash", "md5-hash", "nt-hash", "prv", "privkey", "privatekey",
    "private_key", "secret", "psk", "pre-shared-key", "presharedkey", "shared_key", "sharedkey", "otp_seed",
    "authorizedkeys", "ldap_bindpw", "key", "apikey", "api_key", "token", "community", "radius_secret", "tls",
)  # fmt: skip
_SECRET_RE = re.compile(rf"<({'|'.join(re.escape(t) for t in _SECRET_TAGS)})(\s[^>]*)?>([^<]+)</\1>", re.IGNORECASE)


def redact_config(text: str) -> str:
    """config.xml without its secrets: each one becomes *** (so changes to them still show)."""
    return _SECRET_RE.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}>***</{m.group(1)}>", text)


class OPNsenseError(AerohiveConnectionError):
    """The router couldn't be reached, or refused the request."""


class OPNsenseAuthError(AerohiveAuthError, OPNsenseError):
    """The API key or secret was refused."""


class OPNsenseClient:
    """A small async client for OPNsense's REST API.

    It has the attributes the rest of the integration reads from an SSH client
    (host, hostname, dialect, connected...), so the same coordinator can poll it;
    CLI commands aren't available.
    """

    dialect = DIALECT_OPNSENSE
    config_unsaved = None

    def __init__(self, session: Any, host: str, port: int, key: str, secret: str, *, verify_ssl: bool = True) -> None:
        import aiohttp  # noqa: PLC0415

        self._session = session
        self.host = host
        self.port = port
        self.hostname: str | None = None
        self.connected = True
        self._auth = aiohttp.BasicAuth(key, secret)
        self._ssl = None if verify_ssl else False
        self._base = f"https://{host}:{port}/api" if port != 443 else f"https://{host}/api"

    async def request(self, method: str, path: str, payload: Any = None, *, timeout: float = 20) -> Any:
        import aiohttp  # noqa: PLC0415

        url = f"{self._base}/{path.lstrip('/')}"
        try:
            async with self._session.request(
                method, url, auth=self._auth, json=payload, ssl=self._ssl, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status in (401, 403):
                    raise OPNsenseAuthError(f"{self.host} refused the API key (HTTP {response.status})")
                if response.status >= 400:
                    raise OPNsenseError(f"{self.host}: HTTP {response.status} for {path}")
                try:
                    return await response.json(content_type=None)
                except ValueError as err:
                    raise OPNsenseError(f"{self.host}: {path} didn't answer with JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OPNsenseError(f"Cannot reach {self.host}: {err}") from err

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def post(self, path: str, payload: Any = None) -> Any:
        return await self.request("POST", path, payload if payload is not None else {})

    async def first(self, paths: tuple[str, ...]) -> Any:
        """The first of several endpoints that exists (e.g. whichever DHCP server is installed)."""
        for path in paths:
            try:
                payload = await self.post(path, {"current": 1, "rowCount": -1})
            except OPNsenseAuthError:
                raise
            except OPNsenseError:
                continue
            if _rows(payload) or (isinstance(payload, dict) and "rows" in payload):
                return payload
        return None

    # The rest of the integration talks to "a client"; OPNsense has no CLI.
    async def connect(self) -> None:
        self.connected = True

    async def config_text(self) -> str:
        """The current configuration (config.xml) with passwords, keys and secrets removed."""
        import aiohttp  # noqa: PLC0415

        url = f"{self._base}/{CONFIG_DOWNLOAD}"
        try:
            async with self._session.request(
                "GET", url, auth=self._auth, ssl=self._ssl, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status in (401, 403):
                    raise OPNsenseAuthError(f"{self.host} refused the API key for the configuration (HTTP {response.status})")
                if response.status >= 400:
                    raise OPNsenseError(f"{self.host}: HTTP {response.status} for {CONFIG_DOWNLOAD}")
                text = await response.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OPNsenseError(f"Cannot reach {self.host}: {err}") from err
        if "<opnsense" not in text:
            raise OPNsenseError(f"{self.host} didn't return its configuration")
        return redact_config(text)

    async def run(self, command: str, **kwargs: Any) -> str:
        if command == CONFIG_COMMAND:
            return await self.config_text()
        raise OPNsenseError("OPNsense is managed through its API; CLI commands aren't available")

    async def run_sequence(self, commands: Any, **kwargs: Any) -> list[str]:
        raise OPNsenseError("OPNsense is managed through its API; CLI commands aren't available")

    async def async_set_dialect(self, dialect: str) -> None:
        return

    async def reboot(self, command: str = "", *, save: bool = False) -> None:
        await self.post(REBOOT)

    async def close(self) -> None:
        self.connected = False
