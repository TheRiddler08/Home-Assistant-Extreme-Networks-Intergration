"""OpenWrt over SSH: commands and parsers (no Home Assistant imports).

OpenWrt is read with its own JSON interfaces (``ubus call ...``) and UCI, not
screen-scraped. Wi-Fi changes use UCI (``uci set`` / ``uci commit wireless``)
followed by ``wifi reload``; network, firewall and DHCP settings are never
changed.

Secrets: Wi-Fi keys are never read into Home Assistant. ``uci show`` output is
filtered on the router, backups mask key/password options, and a key copied to
a new radio is copied on the router itself.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from .commands import InvalidArgument, validate_psk, validate_ssid_name

DIALECT_OPENWRT = "openwrt"

BOARD = "ubus call system board"
SYSTEM_INFO = "ubus call system info"
NETWORK_DEVICES = "ubus call network.device status"
NETWORK_INTERFACES = "ubus call network.interface dump"
WIRELESS_STATUS = "ubus call network.wireless status"
CPU_STAT = "head -n 1 /proc/stat"
TEMPERATURE = "cat /sys/class/thermal/thermal_zone0/temp"
DHCP_LEASES = "cat /tmp/dhcp.leases"
ARP = "cat /proc/net/arp"
UCI_CHANGES = "uci changes"
# Secret options never leave the router.
_SECRET_OPTIONS = "key|password|sae_password|auth_secret|acct_secret|priv_key_pwd|private_key|preshared_key|psk|secret"
UCI_WIRELESS = f"uci -X show wireless | grep -vE '\\.({_SECRET_OPTIONS})='"
# Backups: everything, with secret values masked (restoring them would set the key to '***').
CONFIG_EXPORT = f"uci export | sed -E \"s/^([[:space:]]*option ({_SECRET_OPTIONS})) .*/\\1 '***'/\""
# USB devices from sysfs (root hubs, named usbN, are skipped).
USB_DEVICES = (
    "for d in /sys/bus/usb/devices/*-*; do [ -f \"$d/idVendor\" ] && "
    "echo \"$(cat \"$d/idVendor\"):$(cat \"$d/idProduct\") $(cat \"$d/product\" 2>/dev/null)\"; done; true"
)
REBOOT = "reboot"
WIFI_APPLY = "uci commit wireless && wifi reload"
FAILED_MARK = "ERROR: OpenWrt did not accept the change"

_IFNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")
_SECTION_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_RADIO_RE = re.compile(r"^radio\d+$")
_DSA_PORT_RE = re.compile(r"^(lan\d*|wan\d*|sfp\d*)$")
_ETH_PORT_RE = re.compile(r"^eth\d+$")


def iwinfo_info(device: str) -> str:
    return f"ubus call iwinfo info '{{\"device\":\"{_ifname(device)}\"}}'"


def iwinfo_assoclist(device: str) -> str:
    return f"ubus call iwinfo assoclist '{{\"device\":\"{_ifname(device)}\"}}'"


def _ifname(name: str) -> str:
    if not _IFNAME_RE.match(name or ""):
        raise InvalidArgument(f"Invalid interface name: {name!r}")
    return name


def _section(name: str) -> str:
    if not _SECTION_RE.match(name or ""):
        raise InvalidArgument(f"Invalid UCI section: {name!r}")
    return name


def _radio(name: str) -> str:
    if not _RADIO_RE.match(name or ""):
        raise InvalidArgument(f"Invalid radio: {name!r}")
    return name


def load_json(text: str) -> Any:
    """JSON from a ubus reply (the shell may add a stray line before it)."""
    text = (text or "").strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


@dataclass
class Board:
    hostname: str | None = None
    model: str | None = None
    board_name: str | None = None
    version: str | None = None  # "23.05.3"
    revision: str | None = None  # "r23809-234f1a2efa"
    description: str | None = None  # "OpenWrt 23.05.3 r23809-234f1a2efa"
    target: str | None = None
    kernel: str | None = None


def parse_board(text: str) -> Board:
    data = _dict(load_json(text))
    release = _dict(data.get("release"))
    return Board(
        hostname=data.get("hostname"),
        model=data.get("model"),
        board_name=data.get("board_name"),
        version=release.get("version"),
        revision=release.get("revision"),
        description=release.get("description"),
        target=release.get("target"),
        kernel=data.get("kernel"),
    )


@dataclass
class SystemInfo:
    uptime: int | None = None
    memory_total: int | None = None  # bytes
    memory_available: int | None = None  # bytes
    load1: float | None = None


def parse_system_info(text: str) -> SystemInfo:
    data = _dict(load_json(text))
    memory = _dict(data.get("memory"))
    load = data.get("load") if isinstance(data.get("load"), list) else []
    available = memory.get("available")
    if available is None and memory.get("free") is not None:
        available = memory.get("free", 0) + memory.get("buffered", 0) + memory.get("cached", 0)
    return SystemInfo(
        uptime=data.get("uptime"),
        memory_total=memory.get("total"),
        memory_available=available,
        load1=round(load[0] / 65536, 2) if load else None,
    )


def parse_cpu_stat(text: str) -> tuple[int, int] | None:
    """'cpu  4705 356 584 3699 23 23 0 0 0 0' -> (busy, total) jiffies."""
    match = re.search(r"^cpu\s+([\d\s]+)$", (text or "").strip(), re.MULTILINE)
    if not match:
        return None
    values = [int(v) for v in match.group(1).split()]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if not previous or not current:
        return None
    busy, total = current[0] - previous[0], current[1] - previous[1]
    if total <= 0 or busy < 0:
        return None
    return round(100 * busy / total, 1)


def parse_temperature(text: str) -> float | None:
    match = re.match(r"^\s*(-?\d+)\s*$", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return round(value / 1000, 1) if abs(value) > 200 else float(value)


# --------------------------------------------------------------------------
# Ethernet ports
# --------------------------------------------------------------------------


@dataclass
class Port:
    name: str
    up: bool | None = None
    carrier: bool | None = None
    speed: int | None = None  # Mbit/s
    duplex: str | None = None
    mac: str | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    wan: bool = False


def _speed(value: Any) -> tuple[int | None, str | None]:
    """'1000F' -> (1000, 'Full'); '-1F' / None -> (None, None)."""
    match = re.match(r"^(-?\d+)([FH])?$", str(value or ""))
    if not match or int(match.group(1)) <= 0:
        return None, None
    return int(match.group(1)), {"F": "Full", "H": "Half"}.get(match.group(2) or "")


def parse_ports(devices_text: str, interfaces_text: str = "") -> dict[str, Port]:
    """Physical Ethernet ports from 'ubus call network.device status'.

    Devices with switch-port names (lan1..lan4, wan: DSA) are the ports when
    there are any; otherwise the plain ethN devices are (x86 and older targets).
    Bridges, VLANs, Wi-Fi and tunnels are skipped. A port is the WAN port when
    it's named wan* or the 'wan' interface uses it.
    """
    devices = load_json(devices_text)
    if not isinstance(devices, dict):
        return {}
    wan_devices = set()
    for iface in _dicts((_dict(load_json(interfaces_text))).get("interface")):
        if str(iface.get("interface", "")).startswith("wan"):
            for key in ("l3_device", "device"):
                if iface.get(key):
                    wan_devices.add(iface[key])
    names = [n for n, d in devices.items() if isinstance(d, dict) and not d.get("wireless") and d.get("type") in (None, "Network device")]
    dsa = [n for n in names if _DSA_PORT_RE.match(n)]
    chosen = dsa if dsa else [n for n in names if _ETH_PORT_RE.match(n)]
    result: dict[str, Port] = {}
    for name in chosen:
        dev = devices[name]
        stats = dev.get("statistics") or {}
        speed, duplex = _speed(dev.get("speed"))
        carrier = dev.get("carrier")
        result[name] = Port(
            name=name,
            up=dev.get("up"),
            carrier=carrier if carrier is not None else dev.get("up"),
            speed=speed,
            duplex=duplex,
            mac=dev.get("macaddr"),
            rx_bytes=stats.get("rx_bytes"),
            tx_bytes=stats.get("tx_bytes"),
            rx_packets=stats.get("rx_packets"),
            tx_packets=stats.get("tx_packets"),
            rx_errors=stats.get("rx_errors"),
            tx_errors=stats.get("tx_errors"),
            wan=name.startswith("wan") or name in wan_devices,
        )
    return result


def bridge_mac(devices_text: str) -> str | None:
    """The router's own MAC: br-lan, else the first port."""
    devices = _dict(load_json(devices_text))
    for name in ("br-lan", "eth0", "lan1", "lan"):
        if isinstance(devices.get(name), dict) and devices[name].get("macaddr"):
            return devices[name]["macaddr"]
    return None


@dataclass
class IpInterface:
    name: str
    up: bool
    device: str | None
    addresses: list[tuple[str, int]] = field(default_factory=list)


def parse_interfaces(text: str) -> list[IpInterface]:
    result = []
    for iface in _dicts(_dict(load_json(text)).get("interface")):
        result.append(
            IpInterface(
                name=str(iface.get("interface")),
                up=bool(iface.get("up")),
                device=iface.get("l3_device") or iface.get("device"),
                addresses=[
                    (a["address"], int(a.get("mask") or 0)) for a in _dicts(iface.get("ipv4-address")) if a.get("address")
                ],
            )
        )
    return result


# --------------------------------------------------------------------------
# Wi-Fi
# --------------------------------------------------------------------------


@dataclass
class WifiIface:
    section: str
    device: str | None = None  # radio0
    mode: str | None = None
    ssid: str | None = None
    disabled: bool = False
    hidden: bool = False
    encryption: str | None = None
    maxassoc: int | None = None
    network: str | None = None


@dataclass
class WifiRadio:
    name: str
    band: str | None = None  # 2g / 5g / 6g
    channel: str | None = None
    htmode: str | None = None
    disabled: bool = False


def parse_uci_show(text: str) -> dict[str, dict[str, str]]:
    """'wireless.radio0=wifi-device', "wireless.radio0.band='2g'" -> {section: {'.type': ..., option: value}}."""
    sections: dict[str, dict[str, str]] = {}
    for line in (text or "").splitlines():
        match = re.match(r"^\s*[A-Za-z0-9_]+\.([A-Za-z0-9_@\[\]-]+)(?:\.([A-Za-z0-9_]+))?=(.*)$", line)
        if not match:
            continue
        section, option, raw = match.groups()
        try:
            values = shlex.split(raw)
        except ValueError:
            values = [raw.strip("'")]
        value = " ".join(values)
        if option is None:
            sections.setdefault(section, {})[".type"] = value
        elif option not in _SECRET_OPTIONS.split("|"):
            sections.setdefault(section, {})[option] = value
    return sections


def parse_wireless_config(text: str) -> tuple[dict[str, WifiRadio], list[WifiIface]]:
    sections = parse_uci_show(text)
    radios: dict[str, WifiRadio] = {}
    ifaces: list[WifiIface] = []
    for name, opts in sections.items():
        if opts.get(".type") == "wifi-device":
            radios[name] = WifiRadio(
                name=name,
                band=opts.get("band") or {"11g": "2g", "11a": "5g"}.get(opts.get("hwmode", "")),
                channel=opts.get("channel"),
                htmode=opts.get("htmode"),
                disabled=opts.get("disabled") == "1",
            )
        elif opts.get(".type") == "wifi-iface":
            maxassoc = opts.get("maxassoc")
            ifaces.append(
                WifiIface(
                    section=name,
                    device=opts.get("device"),
                    mode=opts.get("mode", "ap"),
                    ssid=opts.get("ssid"),
                    disabled=opts.get("disabled") == "1",
                    hidden=opts.get("hidden") == "1",
                    encryption=opts.get("encryption"),
                    maxassoc=int(maxassoc) if maxassoc and maxassoc.isdigit() else None,
                    network=opts.get("network"),
                )
            )
    return radios, ifaces


@dataclass
class RadioStatus:
    up: bool = False
    ifnames: dict[str, str] = field(default_factory=dict)  # section -> ifname (e.g. phy0-ap0)


def parse_wireless_status(text: str) -> dict[str, RadioStatus]:
    """'ubus call network.wireless status': which radios are up, and each section's ifname."""
    result: dict[str, RadioStatus] = {}
    for radio, info in _dict(load_json(text)).items():
        if not isinstance(info, dict):
            continue
        status = RadioStatus(up=bool(info.get("up")))
        for iface in _dicts(info.get("interfaces")):
            if iface.get("section") and iface.get("ifname"):
                status.ifnames[iface["section"]] = iface["ifname"]
        result[radio] = status
    return result


@dataclass
class IwInfo:
    channel: int | None = None
    frequency: int | None = None
    txpower: int | None = None
    noise: int | None = None
    bitrate: int | None = None
    htmode: str | None = None
    width: int | None = None


def parse_iwinfo_info(text: str) -> IwInfo:
    data = _dict(load_json(text))
    htmode = data.get("htmode")
    width = None
    if htmode and (m := re.search(r"(\d+)$", str(htmode))):
        width = int(m.group(1))
    return IwInfo(
        channel=data.get("channel"),
        frequency=data.get("frequency"),
        txpower=data.get("txpower"),
        noise=data.get("noise") or None,
        bitrate=data.get("bitrate"),
        htmode=htmode,
        width=width,
    )


@dataclass
class Client:
    mac: str
    signal: int | None = None
    noise: int | None = None
    rx_rate_kbps: int | None = None
    tx_rate_kbps: int | None = None
    connected_seconds: int | None = None
    inactive_ms: int | None = None


def parse_assoclist(text: str) -> list[Client]:
    result = []
    for item in _dicts(_dict(load_json(text)).get("results")):
        mac = str(item.get("mac", "")).lower()
        if not mac:
            continue
        result.append(
            Client(
                mac=mac,
                signal=item.get("signal"),
                noise=item.get("noise"),
                rx_rate_kbps=(item.get("rx") or {}).get("rate"),
                tx_rate_kbps=(item.get("tx") or {}).get("rate"),
                connected_seconds=item.get("connected_time"),
                inactive_ms=item.get("inactive"),
            )
        )
    return result


def parse_dhcp_leases(text: str) -> dict[str, tuple[str | None, str | None]]:
    """'/tmp/dhcp.leases': '1731800000 aa:bb:cc:dd:ee:ff 192.168.1.50 phone *' -> mac -> (ip, hostname)."""
    result = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and re.match(r"^[0-9a-f:]{17}$", parts[1].lower()):
            result[parts[1].lower()] = (parts[2], None if parts[3] == "*" else parts[3])
    return result


def parse_arp(text: str) -> list[tuple[str, str, str]]:
    """'/proc/net/arp' -> [(ip, mac, device)] for complete entries."""
    result = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[2] != "0x0" and parts[3] != "00:00:00:00:00:00":
            result.append((parts[0], parts[3].lower(), parts[5]))
    return result


def parse_usb_devices(text: str) -> list[dict[str, str]]:
    """'0bda:8153 USB 10/100/1000 LAN' -> [{'id': '0bda:8153', 'name': 'USB 10/100/1000 LAN'}] (hubs skipped)."""
    result = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)$", line)
        if not m or re.search(r"\bhub\b", m.group(2), re.IGNORECASE):
            continue
        result.append({"id": m.group(1).lower(), "name": m.group(2).strip() or m.group(1).lower()})
    return result


def rate_text(kbps: int | None) -> str | None:
    """866700 -> '866M'."""
    if not kbps:
        return None
    return f"{round(kbps / 1000)}M"


# --------------------------------------------------------------------------
# Wi-Fi changes (shell lines run on the router)
# --------------------------------------------------------------------------


def uci_set(section: str, option: str, value: str) -> str:
    return f"uci set wireless.{_section(section)}.{option}={shlex.quote(str(value))}"


def set_disabled(section: str, disabled: bool) -> str:
    return uci_set(section, "disabled", "1" if disabled else "0")


def set_key(section: str, key: str, encryption: str | None) -> list[str]:
    validate_psk(key)
    cmds = [uci_set(section, "key", key)]
    if not encryption or encryption in ("none", "owe"):
        cmds.append(uci_set(section, "encryption", "psk2"))
    return cmds


def copy_iface(source: str, radio: str) -> str:
    """A new section for ``radio`` with the source's settings, key included, copied on the router."""
    options = "mode network ssid encryption key hidden maxassoc isolate ieee80211w"
    return (
        f"s=$(uci add wireless wifi-iface) && uci set wireless.$s.device={_radio(radio)} && "
        f"for o in {options}; do v=$(uci -q get wireless.{_section(source)}.$o) && uci set wireless.$s.$o=\"$v\"; done; true"
    )


def new_iface(radio: str, ssid: str, key: str | None, hidden: bool, network: str) -> str:
    validate_ssid_name(ssid)
    if key:
        validate_psk(key)
    if not re.match(r"^[A-Za-z0-9_]{1,32}$", network or ""):
        raise InvalidArgument(f"Invalid network: {network!r}")
    parts = [
        "s=$(uci add wireless wifi-iface)",
        f"uci set wireless.$s.device={_radio(radio)}",
        "uci set wireless.$s.mode=ap",
        f"uci set wireless.$s.network={network}",
        f"uci set wireless.$s.ssid={shlex.quote(ssid)}",
        f"uci set wireless.$s.encryption={'psk2' if key else 'none'}",
    ]
    if key:
        parts.append(f"uci set wireless.$s.key={shlex.quote(key)}")
    if hidden:
        parts.append("uci set wireless.$s.hidden=1")
    return " && ".join(parts)


def delete_iface(section: str) -> str:
    return f"uci delete wireless.{_section(section)}"


def apply_line(commands: list[str]) -> str:
    """One shell line: all changes, commit, reload Wi-Fi; prints an error marker if anything failed."""
    return " && ".join([*commands, WIFI_APPLY]) + f" || echo '{FAILED_MARK}'"


def disconnect_client(ifname: str, mac: str) -> str:
    if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", (mac or "").lower()):
        raise InvalidArgument(f"Invalid MAC address: {mac!r}")
    payload = json.dumps({"addr": mac.lower(), "reason": 5, "deauth": True, "ban_time": 0}, separators=(",", ":"))
    return f"ubus call hostapd.{_ifname(ifname)} del_client '{payload}' || echo '{FAILED_MARK}'"


def redact(text: str) -> str:
    """Hide Wi-Fi keys in a uci command line."""
    return re.sub(r"(\.(?:%s)=)('[^']*'|\"[^\"]*\"|\S+)" % _SECRET_OPTIONS, r"\1***", text or "")
