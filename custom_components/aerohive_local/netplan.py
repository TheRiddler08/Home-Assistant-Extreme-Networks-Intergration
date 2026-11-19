"""Network plan: networks (VLANs), Wi-Fi, access rules and per-device keys.

The plan is the *desired* layout of the whole network, edited in the
Network panel's Setup tab. This module is pure Python (no Home Assistant):

- ``validate(plan)`` checks a plan and returns a list of problems.
- ``openwrt_lines(...)``, ``hiveos_commands(...)`` and ``exos_commands(...)``
  turn it into the commands for each device.

Passwords and keys are never part of the plan itself; they're looked up with
``secret(ref)`` (refs ``wifi:<id>`` and ``key:<id>``) from the encrypted vault.

Everything a plan creates on an OpenWrt device lives in UCI sections named
``hn_*``, so applying a plan replaces exactly what an earlier apply created
and nothing else.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .commands import InvalidArgument, quote, validate_psk, validate_radio, validate_ssid_name

SECURITY = ("wpa2", "wpa2_wpa3", "wpa3", "open", "ppsk")
BANDS = ("2.4 GHz", "5 GHz", "6 GHz")
LAN = "lan"  # the router's existing LAN, always reachable as a rule target
WAN_ZONE = "wan"
PREFIX = "hn_"
OPENWRT_CONFIGS = ("network", "dhcp", "firewall", "wireless")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_LEASE_RE = re.compile(r"^\d{1,5}[mhd]?$")
_PORTS_RE = re.compile(r"^\d+(?::\d+)?(?:-\d+(?::\d+)?)?(?:,\d+(?::\d+)?(?:-\d+(?::\d+)?)?)*$")
_IFNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,12}$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_EXOS_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

ENCRYPTION_OPENWRT = {"wpa2": "psk2", "wpa2_wpa3": "sae-mixed", "wpa3": "sae", "open": "none", "ppsk": "psk2"}


def empty_plan() -> dict[str, Any]:
    return {
        "networks": [],
        "wifi": [],
        "keys": [],
        "devices": {"router": None, "router_trunks": ["br-lan"], "switch_trunks": {}, "aps": None},
    }


def normalize(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Fill in missing sections and defaults (older or partial plans)."""
    result = empty_plan()
    plan = plan or {}
    for key in ("networks", "wifi", "keys"):
        result[key] = [dict(item) for item in plan.get(key) or [] if isinstance(item, dict)]
    devices = plan.get("devices") or {}
    result["devices"].update({k: v for k, v in devices.items() if k in result["devices"]})
    for net in result["networks"]:
        net.setdefault("dhcp", True)
        net.setdefault("dhcp_start", 50)
        net.setdefault("dhcp_limit", 150)
        net.setdefault("lease", "12h")
        net.setdefault("internet", True)
        net.setdefault("router_access", False)
        net.setdefault("reach", [])
        net.setdefault("switch_ports", {})
    for wifi in result["wifi"]:
        wifi.setdefault("security", "wpa2")
        wifi.setdefault("bands", ["2.4 GHz", "5 GHz"])
        wifi.setdefault("hidden", False)
        wifi.setdefault("isolate", False)
        wifi.setdefault("max_clients", None)
        wifi.setdefault("enabled", True)
    for key in result["keys"]:
        key.setdefault("network", None)
        key.setdefault("mac", None)
        key.setdefault("expires", None)
    return result


def secret_refs(plan: dict[str, Any]) -> set[str]:
    return {f"wifi:{w['id']}" for w in plan["wifi"]} | {f"key:{k['id']}" for k in plan["keys"]}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _interface(net: dict[str, Any]) -> ipaddress.IPv4Interface:
    iface = ipaddress.IPv4Interface(str(net.get("subnet") or ""))
    if iface.network.prefixlen > 30 or iface.ip in (iface.network.network_address, iface.network.broadcast_address):
        raise ValueError("needs the router's own address, e.g. 10.10.10.1/24")
    return iface


def validate(plan: dict[str, Any], secret: Callable[[str], str | None] | None = None) -> list[str]:
    """Problems that stop the plan from being applied (empty list: fine)."""
    errors: list[str] = []
    nets = plan["networks"]
    ids: set[str] = set()
    vlans: dict[int, str] = {}
    subnets: list[tuple[ipaddress.IPv4Network, str]] = []
    for net in nets:
        nid, name = net.get("id"), net.get("name") or net.get("id")
        if not isinstance(nid, str) or not _ID_RE.match(nid) or nid == LAN:
            errors.append(f"Network {name!r}: the ID must be 1-16 lowercase letters, digits or _ (and not 'lan')")
            continue
        if nid in ids:
            errors.append(f"Network ID {nid!r} is used twice")
        ids.add(nid)
        vid = net.get("vlan")
        if not isinstance(vid, int) or isinstance(vid, bool) or not 2 <= vid <= 4094:
            errors.append(f"{name}: the VLAN must be 2-4094")
        elif vid in vlans:
            errors.append(f"{name}: VLAN {vid} is also used by {vlans[vid]}")
        else:
            vlans[vid] = name
        try:
            iface = _interface(net)
        except ValueError as err:
            errors.append(f"{name}: invalid subnet {net.get('subnet')!r} ({err})")
        else:
            for other, other_name in subnets:
                if iface.network.overlaps(other):
                    errors.append(f"{name}: {iface.network} overlaps {other_name} ({other})")
            subnets.append((iface.network, name))
            if net.get("dhcp"):
                start, limit = net.get("dhcp_start"), net.get("dhcp_limit")
                hosts = iface.network.num_addresses - 2
                if not (isinstance(start, int) and isinstance(limit, int) and start >= 1 and limit >= 1 and start + limit - 1 <= hosts):
                    errors.append(f"{name}: the DHCP range doesn't fit in {iface.network}")
        if not _LEASE_RE.match(str(net.get("lease") or "")):
            errors.append(f"{name}: lease time must look like 12h, 30m or 1d")
        for port_list in (net.get("switch_ports") or {}).values():
            if port_list and not _PORTS_RE.match(str(port_list).replace(" ", "")):
                errors.append(f"{name}: invalid switch ports {port_list!r} (e.g. 1-8,12)")
    for net in nets:
        for target in net.get("reach") or []:
            if target != LAN and target not in ids:
                errors.append(f"{net.get('name') or net.get('id')}: can reach unknown network {target!r}")
            if target == net.get("id"):
                errors.append(f"{net.get('name')}: a network can't have a rule to itself")

    wifi_ids: set[str] = set()
    ssids: set[str] = set()
    for wifi in plan["wifi"]:
        wid, ssid = wifi.get("id"), wifi.get("ssid") or ""
        if not isinstance(wid, str) or not _ID_RE.match(wid):
            errors.append(f"Wi-Fi {ssid!r}: invalid ID")
            continue
        if wid in wifi_ids:
            errors.append(f"Wi-Fi ID {wid!r} is used twice")
        wifi_ids.add(wid)
        try:
            validate_ssid_name(ssid)
        except InvalidArgument as err:
            errors.append(f"Wi-Fi {ssid!r}: {err}")
        if ssid in ssids:
            errors.append(f"Wi-Fi name {ssid!r} is used twice")
        ssids.add(ssid)
        if wifi.get("network") not in ids:
            errors.append(f"{ssid}: choose the network it belongs to")
        if wifi.get("security") not in SECURITY:
            errors.append(f"{ssid}: unknown security {wifi.get('security')!r}")
        bands = wifi.get("bands") or []
        if not bands or any(b not in BANDS for b in bands):
            errors.append(f"{ssid}: choose at least one band")
        max_clients = wifi.get("max_clients")
        if max_clients is not None and not (isinstance(max_clients, int) and 1 <= max_clients <= 255):
            errors.append(f"{ssid}: max clients must be 1-255")
        if secret is not None and wifi.get("security") in ("wpa2", "wpa2_wpa3", "wpa3"):
            value = secret(f"wifi:{wid}")
            if not value:
                errors.append(f"{ssid}: needs a password")
            else:
                try:
                    validate_psk(value)
                except InvalidArgument as err:
                    errors.append(f"{ssid}: {err}")

    key_ids: set[str] = set()
    for key in plan["keys"]:
        kid, name = key.get("id"), key.get("name") or key.get("id")
        if not isinstance(kid, str) or not _ID_RE.match(kid):
            errors.append(f"Key {name!r}: invalid ID")
            continue
        if kid in key_ids:
            errors.append(f"Key ID {kid!r} is used twice")
        key_ids.add(kid)
        wifi = next((w for w in plan["wifi"] if w.get("id") == key.get("wifi")), None)
        if wifi is None:
            errors.append(f"Key {name}: choose its Wi-Fi network")
        elif wifi.get("security") != "ppsk":
            errors.append(f"Key {name}: {wifi.get('ssid')} doesn't use per-device keys")
        if key.get("network") and key["network"] not in ids:
            errors.append(f"Key {name}: unknown network {key['network']!r}")
        if key.get("mac") and not _MAC_RE.match(str(key["mac"]).lower()):
            errors.append(f"Key {name}: invalid MAC address")
        if key.get("expires"):
            try:
                datetime.fromisoformat(str(key["expires"]))
            except ValueError:
                errors.append(f"Key {name}: invalid expiry time")
        if secret is not None:
            value = secret(f"key:{kid}")
            if not value:
                errors.append(f"Key {name}: no key set")
            else:
                try:
                    validate_psk(value)
                except InvalidArgument as err:
                    errors.append(f"Key {name}: {err}")

    devices = plan["devices"]
    for trunk in devices.get("router_trunks") or []:
        if not _IFNAME_RE.match(str(trunk)):
            errors.append(f"Router trunk {trunk!r}: invalid interface name")
    for port_list in (devices.get("switch_trunks") or {}).values():
        if port_list and not _PORTS_RE.match(str(port_list).replace(" ", "")):
            errors.append(f"Invalid switch trunk ports {port_list!r} (e.g. 49-50)")
    return errors


def _net(plan: dict[str, Any], nid: str | None) -> dict[str, Any] | None:
    return next((n for n in plan["networks"] if n["id"] == nid), None)


def active_keys(plan: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Keys that haven't expired."""
    result = []
    for key in plan["keys"]:
        expires = key.get("expires")
        if expires:
            try:
                if datetime.fromisoformat(str(expires)) <= now:
                    continue
            except ValueError:
                continue
        result.append(key)
    return result


def expired_keys(plan: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    active = {k["id"] for k in active_keys(plan, now)}
    return [k for k in plan["keys"] if k["id"] not in active]


# --------------------------------------------------------------------------
# OpenWrt (UCI)
# --------------------------------------------------------------------------


@dataclass
class DevicePlan:
    """The commands for one device, with what could not be done there."""

    commands: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    managed: dict[str, list[str]] = field(default_factory=dict)


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def _uci(config: str, section: str, option: str | None = None, value: Any = None) -> str:
    if option is None:
        return f"uci set {config}.{section}={value}"
    return f"uci set {config}.{section}.{option}={_q(value)}"


def _uci_list(config: str, section: str, option: str, value: Any) -> str:
    return f"uci add_list {config}.{section}.{option}={_q(value)}"


def openwrt_cleanup(configs: tuple[str, ...] = OPENWRT_CONFIGS) -> list[str]:
    """Remove every section an earlier apply created (``hn_*``)."""
    return [
        f"for s in $(uci -q show {cfg} | sed -n 's/^{cfg}\\.\\({PREFIX}[A-Za-z0-9_]*\\)=.*/\\1/p'); do uci delete {cfg}.$s; done"
        for cfg in configs
    ]


BACKUP_DIR = "/tmp/hn_backup"  # noqa: S108 - on the router
OPENWRT_CONFIRM = f"rm -f {BACKUP_DIR}/pending && echo HN_CONFIRMED"
OPENWRT_RESTORE_NOW = f"sh {BACKUP_DIR}/restore.sh"
_RELOAD = {
    "network": "/etc/init.d/network reload",
    "firewall": "/etc/init.d/firewall reload",
    "dhcp": "/etc/init.d/dnsmasq reload",
    "wireless": "wifi reload",
}


def openwrt_staged(configs: tuple[str, ...] = OPENWRT_CONFIGS) -> str:
    return f"for c in {' '.join(configs)}; do uci -q changes $c; done"


def openwrt_revert(configs: tuple[str, ...] = OPENWRT_CONFIGS) -> str:
    return f"for c in {' '.join(configs)}; do uci revert $c; done"


def _reload_line(configs: tuple[str, ...]) -> str:
    # Wireless changes are picked up by the network reload, so "wifi reload" alone is only for Wi-Fi-only changes.
    return "; ".join(_RELOAD[c] for c in configs if not (c == "wireless" and "network" in configs)) + (
        "; wifi reload" if "network" in configs and "wireless" in configs else ""
    )


def openwrt_reload(configs: tuple[str, ...] = OPENWRT_CONFIGS) -> str:
    """Reload in the background, immune to the SSH session ending (the network may restart)."""
    return f"(trap '' HUP INT TERM; sleep 1; {_reload_line(configs)}) </dev/null >/dev/null 2>&1 &"


def openwrt_prepare(rollback_seconds: int, configs: tuple[str, ...] = OPENWRT_CONFIGS) -> str:
    """Copy the files and write restore.sh / rollback.sh next to them (LuCI-style safe apply)."""
    cfgs = " ".join(configs)
    restore = [
        f"for c in {cfgs}; do cp {BACKUP_DIR}/$c /etc/config/$c; done",
        _reload_line(configs),
        f"rm -f {BACKUP_DIR}/pending",
        "logger -t aerohive_local 'network setup rolled back'",
    ]
    rollback = [f"sleep {int(rollback_seconds)}", f"[ -f {BACKUP_DIR}/pending ] || exit 0", f"sh {BACKUP_DIR}/restore.sh"]
    return (
        f"mkdir -p {BACKUP_DIR} && for c in {cfgs}; do cp /etc/config/$c {BACKUP_DIR}/$c || exit 1; done && "
        f"printf '%s\\n' {' '.join(shlex.quote(x) for x in restore)} > {BACKUP_DIR}/restore.sh && "
        f"printf '%s\\n' {' '.join(shlex.quote(x) for x in rollback)} > {BACKUP_DIR}/rollback.sh"
    )


def openwrt_commit(configs: tuple[str, ...] = OPENWRT_CONFIGS) -> str:
    """Arm the rollback timer (detached, survives the SSH session), then commit."""
    commit = " && ".join(f"uci commit {c}" for c in configs)
    return (
        f"touch {BACKUP_DIR}/pending && "
        f"{{ (trap '' HUP INT TERM; sh {BACKUP_DIR}/rollback.sh) </dev/null >/dev/null 2>&1 & }} && {commit}"
    )


def _radio_short(radio: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", radio)[-6:]


def openwrt_lines(
    plan: dict[str, Any],
    secret: Callable[[str], str | None],
    *,
    is_router: bool,
    radios: dict[str, str | None],
    now: datetime,
    wireless_only: bool = False,
) -> DevicePlan:
    """UCI commands for an OpenWrt router (networks, DHCP, firewall, Wi-Fi) or AP (bridges, Wi-Fi).

    ``radios`` maps radio section (radio0) to its band ("2.4 GHz"/"5 GHz") or None.
    ``wireless_only`` regenerates just the Wi-Fi sections (for key changes).
    """
    out = DevicePlan()
    cmds = out.commands
    cmds += openwrt_cleanup(("wireless",) if wireless_only else OPENWRT_CONFIGS)
    trunks = [t for t in plan["devices"].get("router_trunks") or [] if _IFNAME_RE.match(str(t))] or ["br-lan"]
    used_vids = {n["vlan"] for n in plan["networks"]}
    for net in [] if wireless_only else plan["networks"]:
        vid = net["vlan"]
        dev, iface = f"{PREFIX}br{vid}", f"{PREFIX}if{vid}"
        cmds.append(_uci("network", dev, value="device"))
        cmds.append(_uci("network", dev, "type", "bridge"))
        cmds.append(_uci("network", dev, "name", f"br-hn{vid}"))
        cmds.append(_uci("network", dev, "bridge_empty", "1"))
        for trunk in trunks:
            cmds.append(_uci_list("network", dev, "ports", f"{trunk}.{vid}"))
        cmds.append(_uci("network", iface, value="interface"))
        cmds.append(_uci("network", iface, "device", f"br-hn{vid}"))
        if is_router:
            addr = _interface(net)
            cmds.append(_uci("network", iface, "proto", "static"))
            cmds.append(_uci("network", iface, "ipaddr", str(addr.ip)))
            cmds.append(_uci("network", iface, "netmask", str(addr.netmask)))
            dhcp = f"{PREFIX}dhcp{vid}"
            cmds.append(_uci("dhcp", dhcp, value="dhcp"))
            cmds.append(_uci("dhcp", dhcp, "interface", iface))
            if net.get("dhcp"):
                cmds.append(_uci("dhcp", dhcp, "start", net["dhcp_start"]))
                cmds.append(_uci("dhcp", dhcp, "limit", net["dhcp_limit"]))
                cmds.append(_uci("dhcp", dhcp, "leasetime", net["lease"]))
            else:
                cmds.append(_uci("dhcp", dhcp, "ignore", "1"))
            zone, zname = f"{PREFIX}zone{vid}", f"hn{vid}"
            cmds.append(_uci("firewall", zone, value="zone"))
            cmds.append(_uci("firewall", zone, "name", zname))
            cmds.append(_uci_list("firewall", zone, "network", iface))
            cmds.append(_uci("firewall", zone, "input", "ACCEPT" if net.get("router_access") else "REJECT"))
            cmds.append(_uci("firewall", zone, "output", "ACCEPT"))
            cmds.append(_uci("firewall", zone, "forward", "REJECT"))
            if not net.get("router_access"):
                for proto, port, label in (("udp", "67-68", "dhcp"), ("tcp udp", "53", "dns")):
                    rule = f"{PREFIX}rule{vid}_{label}"
                    cmds.append(_uci("firewall", rule, value="rule"))
                    cmds.append(_uci("firewall", rule, "name", f"hn{vid} {label.upper()}"))
                    cmds.append(_uci("firewall", rule, "src", zname))
                    cmds.append(_uci("firewall", rule, "proto", proto))
                    cmds.append(_uci("firewall", rule, "dest_port", port))
                    cmds.append(_uci("firewall", rule, "target", "ACCEPT"))
            targets = ([WAN_ZONE] if net.get("internet") else []) + list(net.get("reach") or [])
            for target in targets:
                if target in (WAN_ZONE, LAN):
                    dest, label = target, target
                else:
                    other = _net(plan, target)
                    if other is None or other["vlan"] not in used_vids:
                        continue
                    dest, label = f"hn{other['vlan']}", str(other["vlan"])
                fwd = f"{PREFIX}fwd{vid}_{label}"
                cmds.append(_uci("firewall", fwd, value="forwarding"))
                cmds.append(_uci("firewall", fwd, "src", zname))
                cmds.append(_uci("firewall", fwd, "dest", dest))
        else:
            cmds.append(_uci("network", iface, "proto", "none"))

    keys = active_keys(plan, now)
    for wifi in plan["wifi"]:
        net = _net(plan, wifi["network"])
        if net is None:
            continue
        wanted = [r for r, band in radios.items() if band in wifi["bands"] or band is None and len(wifi["bands"]) == len(BANDS)]
        missing = [b for b in wifi["bands"] if b not in radios.values()]
        if missing and radios:
            out.warnings.append(f"{wifi['ssid']}: this device has no {', '.join(missing)} radio")
        security = wifi["security"]
        key = secret(f"wifi:{wifi['id']}") if security != "open" else None
        if security != "open" and not key:
            out.errors.append(f"{wifi['ssid']}: no password set")
            continue
        if key:
            out.secrets.append(key)
        for radio in wanted:
            try:
                validate_radio_openwrt(radio)
            except InvalidArgument as err:
                out.errors.append(str(err))
                continue
            sec = f"{PREFIX}w_{wifi['id']}_{_radio_short(radio)}"
            cmds.append(_uci("wireless", sec, value="wifi-iface"))
            cmds.append(_uci("wireless", sec, "device", radio))
            cmds.append(_uci("wireless", sec, "mode", "ap"))
            cmds.append(_uci("wireless", sec, "network", f"{PREFIX}if{net['vlan']}"))
            cmds.append(_uci("wireless", sec, "ssid", wifi["ssid"]))
            cmds.append(_uci("wireless", sec, "encryption", ENCRYPTION_OPENWRT[security]))
            if key:
                cmds.append(_uci("wireless", sec, "key", key))
            if wifi.get("hidden"):
                cmds.append(_uci("wireless", sec, "hidden", "1"))
            if wifi.get("isolate"):
                cmds.append(_uci("wireless", sec, "isolate", "1"))
            if wifi.get("max_clients"):
                cmds.append(_uci("wireless", sec, "maxassoc", int(wifi["max_clients"])))
            if not wifi.get("enabled", True):
                cmds.append(_uci("wireless", sec, "disabled", "1"))
            if security != "ppsk":
                continue
            vids: set[int] = set()
            for item in [k for k in keys if k["wifi"] == wifi["id"]]:
                value = secret(f"key:{item['id']}")
                if not value:
                    out.errors.append(f"Key {item.get('name') or item['id']}: no key set")
                    continue
                out.secrets.append(value)
                sta = f"{PREFIX}k_{item['id']}_{_radio_short(radio)}"
                cmds.append(_uci("wireless", sta, value="wifi-station"))
                cmds.append(_uci("wireless", sta, "iface", sec))
                cmds.append(_uci("wireless", sta, "key", value))
                if item.get("mac"):
                    cmds.append(_uci("wireless", sta, "mac", str(item["mac"]).lower()))
                other = _net(plan, item.get("network"))
                if other is not None and other["vlan"] != net["vlan"]:
                    cmds.append(_uci("wireless", sta, "vid", other["vlan"]))
                    vids.add(other["vlan"])
            for vid in sorted(vids):
                vsec = f"{PREFIX}v_{wifi['id']}_{_radio_short(radio)}_{vid}"
                cmds.append(_uci("wireless", vsec, value="wifi-vlan"))
                cmds.append(_uci("wireless", vsec, "iface", sec))
                cmds.append(_uci("wireless", vsec, "name", f"v{vid}"))
                cmds.append(_uci("wireless", vsec, "vid", vid))
                cmds.append(_uci("wireless", vsec, "network", f"{PREFIX}if{vid}"))
    return out


def validate_radio_openwrt(radio: str) -> str:
    if not re.match(r"^radio\d+$", radio or ""):
        raise InvalidArgument(f"Invalid radio: {radio!r}")
    return radio


# --------------------------------------------------------------------------
# Aerohive HiveOS access points
# --------------------------------------------------------------------------


def hiveos_commands(
    plan: dict[str, Any],
    secret: Callable[[str], str | None],
    *,
    radios: dict[str, str | None],
    bound: dict[str, set[str]],
    security_objects: dict[str, str],
    managed: list[str],
) -> DevicePlan:
    """CLI commands for an Aerohive AP.

    ``radios``: wifi0 -> band; ``bound``: SSID -> radios it's on now;
    ``security_objects``: SSID -> its security-object; ``managed``: SSIDs an
    earlier apply created (removed when no longer in the plan).

    Each network gets a user profile ``hn<vlan>`` (VLAN ID and attribute = the
    VLAN), and each Wi-Fi's security object points to it, so its clients land
    in that VLAN; the AP tags their traffic on its Ethernet uplink.
    """
    out = DevicePlan()
    cmds = out.commands
    profiles_done: set[int] = set()
    kept: list[str] = []
    for wifi in plan["wifi"]:
        ssid, security = wifi["ssid"], wifi["security"]
        net = _net(plan, wifi["network"])
        if net is None:
            continue
        if security == "ppsk":
            out.warnings.append(f"{ssid}: per-device keys on Aerohive access points aren't supported yet; skipped here")
            continue
        if security == "wpa3":
            out.warnings.append(f"{ssid}: WPA3-only isn't set on Aerohive access points yet; skipped here (use WPA2 or WPA2/WPA3)")
            continue
        if wifi.get("isolate"):
            out.warnings.append(f"{ssid}: client isolation isn't set on Aerohive access points yet")
        key = secret(f"wifi:{wifi['id']}") if security != "open" else None
        if security != "open" and not key:
            out.errors.append(f"{ssid}: no password set")
            continue
        vid = net["vlan"]
        profile = f"hn{vid}"
        if vid not in profiles_done:
            cmds.append(f"user-profile {profile} qos-policy def-user-qos vlan-id {vid} attribute {vid}")
            profiles_done.add(vid)
        obj = security_objects.get(ssid, ssid)
        q_ssid, q_obj = quote(ssid, "SSID"), quote(obj, "security object")
        cmds.append(f"security-object {q_obj}")
        cmds.append(f"security-object {q_obj} default-user-profile-attr {vid}")
        if key:
            out.secrets.append(key)
            if security == "wpa2_wpa3":
                out.warnings.append(f"{ssid}: set as WPA2 on Aerohive access points")
            cmds.append(f"security-object {q_obj} security protocol-suite wpa2-aes-psk ascii-key {quote(key, 'key')}")
        cmds.append(f"ssid {q_ssid}")
        cmds.append(f"ssid {q_ssid} security-object {q_obj}")
        cmds.append(f"ssid {q_ssid} hide-ssid" if wifi.get("hidden") else f"no ssid {q_ssid} hide-ssid")
        if wifi.get("max_clients"):
            cmds.append(f"ssid {q_ssid} max-client {int(wifi['max_clients'])}")
        want = {r for r, band in radios.items() if wifi.get("enabled", True) and band in wifi["bands"]}
        have = bound.get(ssid, set())
        for radio in sorted(radios):
            validate_radio(radio)
            if radio in want and radio not in have:
                cmds.append(f"interface {radio} ssid {q_ssid}")
            elif radio not in want and radio in have:
                cmds.append(f"no interface {radio} ssid {q_ssid}")
        kept.append(ssid)
    for ssid in managed:
        if ssid in kept or any(w["ssid"] == ssid for w in plan["wifi"]):
            continue
        q_ssid = quote(ssid, "SSID")
        cmds += [f"no interface {r} ssid {q_ssid}" for r in sorted(bound.get(ssid, set()))]
        cmds.append(f"no ssid {q_ssid}")
        obj = security_objects.get(ssid, ssid)
        if obj == ssid:
            cmds.append(f"no security-object {quote(obj, 'security object')}")
    out.managed["ssids"] = kept
    return out


# --------------------------------------------------------------------------
# ExtremeXOS switches
# --------------------------------------------------------------------------


def exos_commands(
    plan: dict[str, Any],
    *,
    entry_id: str,
    existing: dict[str, int | None],
    managed: list[str],
) -> DevicePlan:
    """VLANs on an ExtremeXOS switch: create them, tag them on the trunk ports, set access ports.

    ``existing``: VLAN name -> tag on the switch now. A VLAN that already
    exists with the same tag is reused under its own name.
    """
    out = DevicePlan()
    cmds = out.commands
    trunks = str((plan["devices"].get("switch_trunks") or {}).get(entry_id) or "").replace(" ", "")
    by_tag = {tag: name for name, tag in existing.items() if tag is not None}
    kept: list[str] = []
    for net in plan["networks"]:
        vid = net["vlan"]
        name = by_tag.get(vid) or net["id"]
        if not _EXOS_NAME_RE.match(name):
            out.errors.append(f"{net['name']}: {name!r} isn't a valid ExtremeXOS VLAN name")
            continue
        if name in existing and existing[name] not in (None, vid):
            out.errors.append(f"{net['name']}: VLAN {name} exists on the switch with tag {existing[name]}")
            continue
        q = quote(name, "VLAN")
        if name not in existing:
            cmds.append(f"create vlan {q}")
            cmds.append(f"configure vlan {q} tag {vid}")
        if trunks:
            cmds.append(f"configure vlan {q} add ports {trunks} tagged")
        access = str((net.get("switch_ports") or {}).get(entry_id) or "").replace(" ", "")
        if access:
            if "Default" in existing:
                cmds.append(f"configure vlan Default delete ports {access}")
            cmds.append(f"configure vlan {q} add ports {access} untagged")
        if name not in existing or name in managed:
            kept.append(name)
    if not trunks:
        out.warnings.append("No trunk ports set: the VLANs are created but not carried to the router or access points")
    for name in managed:
        if name not in kept and name in existing:
            cmds.append(f"delete vlan {quote(name, 'VLAN')}")
    out.managed["vlans"] = kept
    return out


# --------------------------------------------------------------------------
# Template: the home layout from the network plan
# --------------------------------------------------------------------------


def template() -> dict[str, Any]:
    """A starting point: management, servers, IoT, guest, home, home5, homeISO, homeOB, wired, Google.

    Addresses follow the plan's convention (.1 router, .50-.199 DHCP). Nothing
    here is applied until you press Apply.
    """

    def net(nid: str, name: str, vlan: int, subnet: str, *, internet: bool = True, router: bool = False, reach: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": nid, "name": name, "vlan": vlan, "subnet": subnet, "dhcp": True, "dhcp_start": 50, "dhcp_limit": 150,
            "lease": "12h", "internet": internet, "router_access": router, "reach": reach or [], "switch_ports": {},
        }

    networks = [
        net("mgmt", "Network management", 10, "10.10.10.1/24", router=True, reach=["servers"]),
        net("servers", "Servers", 20, "10.48.27.1/24"),
        net("iot", "IoT / Smart home", 30, "10.15.19.1/24"),
        net("guest", "Guest Wi-Fi", 40, "10.99.99.1/24"),
        net("home", "home 2.4 GHz", 50, "10.40.33.1/24", reach=["servers", "iot"]),
        net("home5", "home 5 GHz", 60, "10.54.33.1/24", reach=["servers", "iot"]),
        net("iso", "homeISO", 70, "10.99.199.1/24"),
        net("onboard", "homeOB", 80, "10.8.10.1/24", internet=False),
        net("wired", "homeHardlined", 90, "10.41.56.1/24", reach=["servers", "iot"]),
        net("google", "Google-Auth-Home", 110, "10.60.19.1/24"),
    ]
    networks[7]["lease"] = "1h"

    def wifi(wid: str, ssid: str, network: str, security: str, bands: list[str], **extra: Any) -> dict[str, Any]:
        return {"id": wid, "ssid": ssid, "network": network, "security": security, "bands": bands, "hidden": False,
                "isolate": False, "max_clients": None, "enabled": True, **extra}

    return {
        "networks": networks,
        "wifi": [
            wifi("home", "home", "home", "ppsk", ["2.4 GHz"]),
            wifi("home5", "home5", "home5", "ppsk", ["5 GHz"]),
            wifi("homeiso", "homeISO", "iso", "ppsk", ["2.4 GHz"], isolate=True),
            wifi("homeob", "homeOB", "onboard", "wpa2", ["2.4 GHz", "5 GHz"], isolate=True, max_clients=5),
            wifi("guest", "Guest", "guest", "wpa2", ["2.4 GHz", "5 GHz"], isolate=True),
            wifi("google", "Google-Auth-Home", "google", "ppsk", ["2.4 GHz"], hidden=True),
        ],
        "keys": [],
        "devices": {"router": None, "router_trunks": ["br-lan"], "switch_trunks": {}, "aps": None},
    }
