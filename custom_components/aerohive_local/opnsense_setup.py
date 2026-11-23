"""The network setup on an OPNsense router (through its REST API).

For every network in the setup this creates, on the router:

1. a VLAN device ``vlan0.<VLAN>`` on the trunk port (``interfaces/vlan_settings``);
2. an interface assignment ``hn_<id>`` on it with the router's address
   (``interfaces/assignment``, static IPv4);
3. a DHCP range on whichever DHCP server the router uses: Kea
   (``kea/dhcpv4`` subnet) or Dnsmasq (``dnsmasq/settings`` range);
4. firewall rules from the access grid (``firewall/filter``): DHCP and DNS to
   the router, the router itself only if allowed, the networks it may reach,
   other private networks blocked, then the internet if allowed.

Safety, as OPNsense has no rollback through its API:

- Only objects this integration made are touched: VLAN devices named
  ``vlan0.<VLAN>`` on the trunk, interfaces named ``hn_*``, rules and DHCP
  ranges whose description starts with ``[HA]``. LAN, WAN and your own rules
  are never changed, so a mistake can't cut Home Assistant off from the
  router's LAN address.
- New rules only apply to the new interfaces.
- If a step fails, what this apply created is removed again (best effort) and
  the error says what was left.
- After applying, the router is read again to check it's still reachable.

The planning functions are pure; ``async_apply`` does the requests.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

MARK = "[HA]"
PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
_LEASE_RE = re.compile(r"^(\d+)([mhd]?)$")


def vlan_device(vid: int) -> str:
    """OPNsense requires 'vlan0' followed by digits and dots (max 15 characters)."""
    return f"vlan0.{int(vid)}"


def assignment_name(net_id: str) -> str:
    """Interface descriptions may only have letters, digits and underscores."""
    return re.sub(r"[^a-z0-9_]", "_", f"hn_{net_id}".lower())[:32]


def lease_seconds(lease: str | None) -> int:
    match = _LEASE_RE.match(str(lease or "12h"))
    if not match:
        return 43200
    number, unit = int(match.group(1)), match.group(2) or "h"
    return number * {"m": 60, "h": 3600, "d": 86400}[unit]


@dataclass
class Network:
    id: str
    name: str
    vid: int
    address: str  # 10.15.19.1/24
    subnet: str  # 10.15.19.0/24
    pool: tuple[str, str] | None  # DHCP range
    lease: int
    internet: bool
    router_access: bool
    reach: list[str]


def networks(plan: dict[str, Any]) -> list[Network]:
    result = []
    for net in plan["networks"]:
        iface = ipaddress.IPv4Interface(net["subnet"])
        pool = None
        if net.get("dhcp", True):
            hosts = iface.network.network_address
            first = hosts + int(net.get("dhcp_start", 50))
            last = first + int(net.get("dhcp_limit", 150)) - 1
            pool = (str(first), str(last))
        result.append(
            Network(
                id=net["id"],
                name=net.get("name") or net["id"],
                vid=int(net["vlan"]),
                address=str(iface),
                subnet=str(iface.network),
                pool=pool,
                lease=lease_seconds(net.get("lease")),
                internet=bool(net.get("internet", True)),
                router_access=bool(net.get("router_access", False)),
                reach=list(net.get("reach") or []),
            )
        )
    return result


def firewall_rules(
    nets: list[Network], interfaces: dict[str, str]
) -> list[dict[str, Any]]:
    """Rules per network, in order. ``interfaces``: network id -> OPNsense interface (opt3), plus 'lan'."""
    rules: list[dict[str, Any]] = []

    def rule(
        net: Network,
        what: str,
        action: str,
        destination: str,
        *,
        protocol: str = "any",
        port: str = "",
    ) -> None:
        rules.append(
            {
                "enabled": "1",
                "action": action,
                "quick": "1",
                "interface": interfaces[net.id],
                "direction": "in",
                "ipprotocol": "inet",
                "protocol": protocol,
                "source_net": interfaces[net.id],
                "destination_net": destination,
                "destination_port": port,
                "description": f"{MARK} {net.name}: {what}",
            }
        )

    for net in nets:
        if net.id not in interfaces:
            continue
        rule(net, "DHCP", "pass", "any", protocol="UDP", port="67")
        rule(net, "DNS", "pass", "(self)", protocol="TCP/UDP", port="53")
        if net.router_access:
            rule(net, "router", "pass", "(self)")
        else:
            rule(net, "no router access", "block", "(self)")
        for target in net.reach:
            destination = "lan" if target == "lan" else interfaces.get(target)
            if destination:
                other = next((n.name for n in nets if n.id == target), "main LAN")
                rule(net, f"to {other}", "pass", destination)
        for private in PRIVATE_NETS:
            rule(net, "no other networks", "block", private)
        if net.internet:
            rule(net, "internet", "pass", "any")
    return rules


def describe(plan: dict[str, Any], trunks: list[str] | None = None) -> list[str]:
    """What the apply does, for the review (the actual changes are worked out against the router)."""
    lines = []
    if trunks is None:
        trunks = plan["devices"].get("router_trunks")
    names = [t for t in (trunks or []) if t and t != "br-lan"]
    trunk = names[0] if names else "the LAN port"
    nets = networks(plan)
    ifmap = {n.id: assignment_name(n.id) for n in nets} | {"lan": "lan"}
    for net in nets:
        lines.append(f"VLAN {vlan_device(net.vid)} on {trunk} (tag {net.vid})")
        lines.append(
            f"interface {assignment_name(net.id)} on {vlan_device(net.vid)}: static {net.address}"
        )
        if net.pool:
            lines.append(
                f"DHCP {net.pool[0]}-{net.pool[1]} on {assignment_name(net.id)} (lease {net.lease} s)"
            )
    for r in firewall_rules(nets, ifmap):
        port = f" port {r['destination_port']}" if r["destination_port"] else ""
        proto = f" {r['protocol']}" if r["protocol"] != "any" else ""
        lines.append(
            f"firewall {r['action']} {r['source_net']} -> {r['destination_net']}{proto}{port}  ({r['description']})"
        )
    lines.append(
        "remove [HA] rules, DHCP ranges, hn_* interfaces and vlan0.* devices the setup no longer has"
    )
    return lines


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


class SetupError(Exception):
    pass


@dataclass
class Result:
    done: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [r for r in payload["rows"] if isinstance(r, dict)]
    return []


def _token(value: Any) -> str:
    """Search results may show 'igc1 (LAN)' for a device field: the first word is the device."""
    return str(value or "").split(" ")[0]


def _check(payload: Any, what: str) -> str:
    """addItem/setItem answer {"result": "saved", "uuid": ...} or {"result": "failed", "validations": {...}}."""
    if isinstance(payload, dict) and payload.get("result") == "saved":
        return str(payload.get("uuid") or "")
    detail = ""
    if isinstance(payload, dict) and isinstance(payload.get("validations"), dict):
        detail = "; ".join(
            f"{k.split('.')[-1]}: {v}" for k, v in payload["validations"].items()
        )
    elif isinstance(payload, dict) and payload.get("errorMessage"):
        detail = str(payload["errorMessage"])
    raise SetupError(f"{what}: the router refused it{f' ({detail})' if detail else ''}")


def _selected(value: Any) -> list[str]:
    """An option field from a get: {"lan": {"value": "LAN", "selected": 1}, ...} -> selected keys."""
    if isinstance(value, dict):
        return [
            k
            for k, v in value.items()
            if isinstance(v, dict) and str(v.get("selected")) in ("1", "True", "true")
        ]
    if isinstance(value, str):
        return [v for v in value.split(",") if v]
    return []


def _enabled(value: Any) -> bool:
    return str(value) in ("1", "True", "true")


def resolve_trunk(
    configured: list[str] | None, assignments: list[dict[str, Any]]
) -> str:
    """The port the VLANs go on: as set under Devices, or (default 'br-lan' / empty) the LAN port."""
    names = [t for t in (configured or []) if t and t != "br-lan"]
    if names:
        return names[0]
    lan = next(
        (r for r in assignments if str(r.get("identifier") or r.get("uuid")) == "lan"),
        None,
    )
    if lan is None or not _token(lan.get("if")):
        raise SetupError(
            "couldn't find the router's LAN port; enter the trunk port (e.g. igc1) under Devices"
        )
    return _token(lan.get("if"))


SEARCH = {"current": 1, "rowCount": -1}
SLOW = 120  # interface and filter reloads can take a while


async def _dhcp_server(client: Any) -> str | None:
    """'kea' or 'dnsmasq', whichever is enabled (Kea first)."""
    try:
        kea = await client.get("kea/dhcpv4/get")
        if _enabled(
            ((kea or {}).get("dhcpv4") or {}).get("general", {}).get("enabled")
        ):
            return "kea"
    except Exception:  # noqa: BLE001 - not installed / no access
        pass
    try:
        dnsmasq = await client.get("dnsmasq/settings/get")
        if _enabled(((dnsmasq or {}).get("dnsmasq") or {}).get("enable")):
            return "dnsmasq"
    except Exception:  # noqa: BLE001
        pass
    return None


async def async_apply(
    client: Any, plan: dict[str, Any], configured_trunks: list[str] | None = None
) -> Result:
    """Make the router match the setup. Raises SetupError (after undoing what it created)."""
    result = Result()
    nets = networks(plan)
    wanted_vids = {n.vid for n in nets}
    created: list[tuple[str, str]] = []  # (delete endpoint, id) to undo on failure
    restore: list[tuple[str, dict[str, Any]]] = []  # settings to put back on failure

    async def call(path: str, payload: Any = None) -> Any:
        return await client.post(path, payload)

    async def reload(path: str) -> None:
        answer = await client.request("POST", path, {}, timeout=SLOW)
        if isinstance(answer, dict) and str(answer.get("status", "ok")).lower() not in (
            "ok",
            "done",
            "",
        ):
            raise SetupError(
                f"{path.split('/')[0]}: applying the change failed on the router ({answer.get('status')})"
            )

    async def undo() -> None:
        for path, payload in restore:
            try:
                await call(path, payload)
            except Exception as err:  # noqa: BLE001
                result.warnings.append(f"couldn't put back {path}: {err}")
        for endpoint, item in reversed(created):
            if item == "-":
                continue
            try:
                await call(f"{endpoint}/{item}")
            except Exception as err:  # noqa: BLE001
                result.warnings.append(f"couldn't remove {item}: {err}")
        kinds = {
            endpoint.split("/")[0] + "/" + endpoint.split("/")[1]
            for endpoint, _ in created
        }
        for kind, path in (
            ("firewall/filter", "firewall/filter/apply"),
            ("kea/dhcpv4", "kea/service/reconfigure"),
            ("dnsmasq/settings", "dnsmasq/service/reconfigure"),
            ("interfaces/assignment", "interfaces/assignment/reconfigure"),
            ("interfaces/vlan_settings", "interfaces/vlan_settings/reconfigure"),
        ):
            if kind in kinds:
                try:
                    await client.request("POST", path, {}, timeout=SLOW)
                except Exception:  # noqa: BLE001
                    pass

    try:
        assigned = _rows(await call("interfaces/assignment/search_item", SEARCH))
        trunk = resolve_trunk(configured_trunks, assigned)
        result.done.append(f"trunk {trunk}")

        # 1. VLAN devices on the trunk (an existing VLAN with the same tag there is used as it is).
        vlans = _rows(await call("interfaces/vlan_settings/search_item", SEARCH))
        device_of: dict[str, str] = {}
        new_vlans = False
        for net in nets:
            same = next(
                (
                    r
                    for r in vlans
                    if _token(r.get("if")) == trunk
                    and str(r.get("tag")) == str(net.vid)
                ),
                None,
            )
            if same is not None:
                device_of[net.id] = _token(same.get("vlanif"))
                continue
            device = vlan_device(net.vid)
            if any(_token(r.get("vlanif")) == device for r in vlans):
                raise SetupError(
                    f"{device} already exists on another port; remove it or change the trunk port"
                )
            uuid = _check(
                await call(
                    "interfaces/vlan_settings/add_item",
                    {
                        "vlan": {
                            "if": trunk,
                            "tag": str(net.vid),
                            "vlanif": device,
                            "pcp": "0",
                            "descr": f"{MARK} {net.name}",
                        }
                    },
                ),
                f"VLAN {net.vid}",
            )
            created.append(("interfaces/vlan_settings/del_item", uuid))
            device_of[net.id] = device
            result.done.append(f"VLAN {device}")
            new_vlans = True
        if new_vlans:
            await reload("interfaces/vlan_settings/reconfigure")

        # 2. Interfaces with the router's address on each VLAN.
        by_device = {_token(r.get("if")): r for r in assigned}
        interfaces: dict[str, str] = {"lan": "lan"}
        changed = False
        for net in nets:
            device, name = device_of[net.id], assignment_name(net.id)
            item = {
                "interface": {
                    "descr": name,
                    "if": device,
                    "enable": "1",
                    "lock": "0",
                    "type4": "staticv4",
                    "ipaddr": net.address,
                }
            }
            row = by_device.get(device)
            if row is None:
                key = _check(
                    await call("interfaces/assignment/add_item", item),
                    f"interface for {net.name}",
                )
                created.append(("interfaces/assignment/del_item", key))
                result.done.append(f"interface {name} {net.address}")
                changed = True
            else:
                key = str(row.get("identifier") or row.get("uuid"))
                descr = str(row.get("descr") or row.get("description") or "")
                if not descr.lower().startswith("hn_"):
                    raise SetupError(
                        f"{device} is already in use as interface {descr or key}; remove that assignment or pick another VLAN"
                    )
                if (
                    str(row.get("ipaddr") or "") != net.address
                    or not _enabled(row.get("enable", "1"))
                    or descr != name
                ):
                    _check(
                        await call(f"interfaces/assignment/set_item/{key}", item),
                        f"interface for {net.name}",
                    )
                    result.done.append(f"interface {name} {net.address}")
                    changed = True
            interfaces[net.id] = key
        if changed:
            await reload("interfaces/assignment/reconfigure")

        stale = [
            r
            for r in assigned
            if str(r.get("descr") or r.get("description") or "")
            .lower()
            .startswith("hn_")
            and _token(r.get("if")) not in device_of.values()
        ]
        stale_keys = {str(r.get("identifier") or r.get("uuid")) for r in stale}

        # 3. DHCP on whichever server is in use: our ranges are replaced.
        server = await _dhcp_server(client)
        wanted = [interfaces[n.id] for n in nets if n.pool]
        if server is None:
            if wanted:
                result.warnings.append(
                    "No DHCP server is enabled on the router (Kea or Dnsmasq): devices won't get addresses"
                )
        elif server == "kea":
            for row in _rows(await call("kea/dhcpv4/search_subnet", SEARCH)):
                if str(row.get("description", "")).startswith(MARK):
                    await call(f"kea/dhcpv4/del_subnet/{row['uuid']}")
            for net in nets:
                if net.pool:
                    subnet = {
                        "subnet4": {
                            "subnet": net.subnet,
                            "interface": interfaces[net.id],
                            "pools": f"{net.pool[0]}-{net.pool[1]}",
                            "option_data_autocollect": "1",
                            "valid_lifetime": str(net.lease),
                            "description": f"{MARK} {net.name}",
                        }
                    }
                    created.append(
                        (
                            "kea/dhcpv4/del_subnet",
                            _check(
                                await call("kea/dhcpv4/add_subnet", subnet),
                                f"DHCP for {net.name}",
                            ),
                        )
                    )
            general = (
                ((await client.get("kea/dhcpv4/get")) or {}).get("dhcpv4") or {}
            ).get("general") or {}
            listen = _selected(general.get("interfaces"))
            new_listen = sorted((set(listen) - stale_keys) | set(wanted))
            if new_listen != sorted(listen):
                restore.append(
                    (
                        "kea/dhcpv4/set",
                        {"dhcpv4": {"general": {"interfaces": ",".join(listen)}}},
                    )
                )
                created.append(("kea/dhcpv4", "-"))  # so the undo reconfigures Kea
                _check(
                    await call(
                        "kea/dhcpv4/set",
                        {"dhcpv4": {"general": {"interfaces": ",".join(new_listen)}}},
                    ),
                    "Kea interfaces",
                )
            await reload("kea/service/reconfigure")
            result.done.append("DHCP (Kea)")
        else:
            for row in _rows(await call("dnsmasq/settings/search_range", SEARCH)):
                if str(row.get("description", "")).startswith(MARK):
                    await call(f"dnsmasq/settings/del_range/{row['uuid']}")
            for net in nets:
                if net.pool:
                    item = {
                        "range": {
                            "interface": interfaces[net.id],
                            "start_addr": net.pool[0],
                            "end_addr": net.pool[1],
                            "lease_time": str(net.lease),
                            "description": f"{MARK} {net.name}",
                        }
                    }
                    created.append(
                        (
                            "dnsmasq/settings/del_range",
                            _check(
                                await call("dnsmasq/settings/add_range", item),
                                f"DHCP for {net.name}",
                            ),
                        )
                    )
            settings = (
                ((await client.get("dnsmasq/settings/get")) or {}).get("dnsmasq")
            ) or {}
            listen = _selected(settings.get("interface"))
            new_listen = sorted((set(listen) - stale_keys) | set(wanted))
            if listen and new_listen != sorted(
                listen
            ):  # nothing selected = every interface
                restore.append(
                    (
                        "dnsmasq/settings/set",
                        {"dnsmasq": {"interface": ",".join(listen)}},
                    )
                )
                created.append(("dnsmasq/settings", "-"))
                _check(
                    await call(
                        "dnsmasq/settings/set",
                        {"dnsmasq": {"interface": ",".join(new_listen)}},
                    ),
                    "Dnsmasq interfaces",
                )
            await reload("dnsmasq/service/reconfigure")
            result.done.append("DHCP (Dnsmasq)")

        # 4. Firewall: our rules are replaced (only on our interfaces). The new
        # ones are added before the old ones go, so a failure leaves the old set.
        old_rules = [
            r
            for r in _rows(await call("firewall/filter/search_rule", SEARCH))
            if str(r.get("description", "")).startswith(MARK)
        ]
        rules = firewall_rules(nets, interfaces)
        for item in rules:
            created.append(
                (
                    "firewall/filter/del_rule",
                    _check(
                        await call("firewall/filter/add_rule", {"rule": item}),
                        item["description"],
                    ),
                )
            )
        for row in old_rules:
            await call(f"firewall/filter/del_rule/{row['uuid']}")
        await reload("firewall/filter/apply")
        result.done.append(f"{len(rules)} firewall rules")

        # 5. Networks that left the setup: their interface, then their VLAN device.
        for r in stale:
            await call(
                f"interfaces/assignment/del_item/{r.get('identifier') or r.get('uuid')}"
            )
            result.done.append(
                f"removed interface {r.get('descr') or r.get('description')}"
            )
        if stale:
            await reload("interfaces/assignment/reconfigure")
        removed = False
        for r in vlans:
            tag = str(r.get("tag") or "")
            if (
                str(r.get("descr", "")).startswith(MARK)
                and tag.isdigit()
                and int(tag) not in wanted_vids
            ):
                await call(f"interfaces/vlan_settings/del_item/{r['uuid']}")
                result.done.append(f"removed VLAN {_token(r.get('vlanif'))}")
                removed = True
        if removed:
            await reload("interfaces/vlan_settings/reconfigure")
    except Exception as err:
        await undo()
        if isinstance(err, SetupError):
            raise
        if type(err).__name__ == "OPNsenseAuthError":
            raise SetupError(
                "the API key may not change this (give its user the Interfaces, Firewall and DHCP pages, or 'All pages')"
            ) from err
        raise SetupError(str(err)) from err
    return result
