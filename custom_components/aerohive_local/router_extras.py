"""Router extras: fixed IPs (with a DNS name), blocked or paused devices, port forwards.

They live in the network plan (``reservations``, ``blocked``, ``forwards``) but are
applied on their own, straight away, to the plan's router: an OpenWrt router (UCI
sections ``hn_res_*``, ``hn_blk_*``, ``hn_fwd_*``) or an OPNsense router (through the
API, marked ``[HA:fixed]`` / ``[HA:block]`` / ``[HA:forward]``). A full network-setup
apply on OpenWrt regenerates them too (``openwrt_lines``).

Only what this integration made is changed; everything else on the router is left
alone.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from datetime import datetime
from typing import Any

PREFIX = "hn_"
FIXED = "[HA:fixed]"
BLOCK = "[HA:block]"
FORWARD = "[HA:forward]"
BLOCK_ALIAS = "HA_blocked"
PROTOCOLS = {"tcp": "TCP", "udp": "UDP", "both": "TCP/UDP"}
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")
SEARCH = {"current": 1, "rowCount": -1}
SLOW = 120


class ExtrasError(Exception):
    pass


def normalize_mac(mac: str) -> str:
    value = str(mac or "").strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", value):
        value = ":".join(value[i : i + 2] for i in range(0, 12, 2))
    if not _MAC_RE.match(value):
        raise ExtrasError(f"{mac!r} isn't a MAC address")
    return value


def hostname(name: str) -> str:
    """A DNS label from a device name ('Living room TV' -> 'living-room-tv')."""
    label = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")
    return label[:63].strip("-")


def _section_id(mac: str) -> str:
    return mac.replace(":", "")


def _ports(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _PORT_RE.match(text):
        raise ExtrasError(
            f"{what}: {value!r} isn't a port or range (e.g. 443 or 8000-8010)"
        )
    for part in text.split("-"):
        if not 1 <= int(part) <= 65535:
            raise ExtrasError(f"{what}: ports go from 1 to 65535")
    return text


def subnets(plan: dict[str, Any]) -> list[ipaddress.IPv4Interface]:
    result = []
    for net in plan.get("networks") or []:
        try:
            result.append(ipaddress.IPv4Interface(net["subnet"]))
        except (KeyError, ValueError):
            continue
    return result


def check_reservation(
    plan: dict[str, Any], mac: str, ip: str, name: str
) -> dict[str, str]:
    mac = normalize_mac(mac)
    try:
        address = ipaddress.IPv4Address(str(ip).strip())
    except ValueError as err:
        raise ExtrasError(f"{ip!r} isn't an IPv4 address") from err
    if not address.is_private:
        raise ExtrasError(f"{address} isn't a private address")
    for iface in subnets(plan):
        if address in iface.network:
            if address in (
                iface.network.network_address,
                iface.network.broadcast_address,
                iface.ip,
            ):
                raise ExtrasError(
                    f"{address} is the network's own address, its broadcast or the router"
                )
            break
    for item in plan.get("reservations") or []:
        if item["ip"] == str(address) and item["mac"] != mac:
            raise ExtrasError(
                f"{address} is already fixed for {item.get('name') or item['mac']}"
            )
    return {"mac": mac, "ip": str(address), "name": str(name or "").strip()[:64]}


def check_forward(item: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()[:48]
    if not name:
        raise ExtrasError("Give the port forward a name")
    proto = str(item.get("protocol") or "tcp").lower()
    if proto not in PROTOCOLS:
        raise ExtrasError(f"{name}: protocol must be TCP, UDP or both")
    port = _ports(item.get("port"), name)
    to_port = _ports(item.get("to_port") or port, name)
    if ("-" in port) != ("-" in to_port):
        raise ExtrasError(f"{name}: forward a range to a range")
    try:
        to_ip = ipaddress.IPv4Address(str(item.get("to_ip") or "").strip())
    except ValueError as err:
        raise ExtrasError(
            f"{name}: {item.get('to_ip')!r} isn't an IPv4 address"
        ) from err
    if not to_ip.is_private:
        raise ExtrasError(
            f"{name}: forward to a device in your network (a private address)"
        )
    fid = (
        re.sub(r"[^a-z0-9_]", "_", str(item.get("id") or name).lower())[:24].strip("_")
        or "fwd"
    )
    for other in others:
        if other["id"] == fid:
            raise ExtrasError(f"Two port forwards are called {name}")
        if other["port"] == port and (
            other["protocol"] == proto or "both" in (other["protocol"], proto)
        ):
            raise ExtrasError(
                f"{name}: port {port} is already forwarded by {other['name']}"
            )
    return {
        "id": fid,
        "name": name,
        "protocol": proto,
        "port": port,
        "to_ip": str(to_ip),
        "to_port": to_port,
        "enabled": bool(item.get("enabled", True)),
    }


def active_blocks(plan: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    result = []
    for item in plan.get("blocked") or []:
        until = item.get("until")
        if until:
            try:
                if datetime.fromisoformat(until) <= now:
                    continue
            except ValueError:
                pass
        result.append(item)
    return result


# --------------------------------------------------------------------------
# OpenWrt
# --------------------------------------------------------------------------


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def _zone_for(plan: dict[str, Any], ip: str) -> str:
    address = ipaddress.IPv4Address(ip)
    for net in plan.get("networks") or []:
        try:
            if address in ipaddress.IPv4Interface(net["subnet"]).network:
                return f"hn{net['vlan']}"
        except (KeyError, ValueError):
            continue
    return "lan"


def openwrt_lines(plan: dict[str, Any], now: datetime) -> list[str]:
    """UCI commands that add the extras (the caller removes old ``hn_`` sections first)."""
    cmds = []
    for item in plan.get("reservations") or []:
        sec = f"{PREFIX}res_{_section_id(item['mac'])}"
        cmds.append(f"uci set dhcp.{sec}=host")
        cmds.append(f"uci set dhcp.{sec}.mac={_q(item['mac'])}")
        cmds.append(f"uci set dhcp.{sec}.ip={_q(item['ip'])}")
        name = hostname(item.get("name") or "")
        if name:
            cmds.append(f"uci set dhcp.{sec}.name={_q(name)}")
            cmds.append(f"uci set dhcp.{sec}.dns='1'")
    for item in active_blocks(plan, now):
        sec = f"{PREFIX}blk_{_section_id(item['mac'])}"
        for kind, target in (("fwd", "forward"), ("in", "input")):
            cmds.append(f"uci set firewall.{sec}{kind}=rule")
            label = f"HA blocked: {item.get('name') or item['mac']}"[:60]
            cmds.append(f"uci set firewall.{sec}{kind}.name={_q(label)}")
            cmds.append(f"uci set firewall.{sec}{kind}.src='*'")
            if target == "forward":
                cmds.append(f"uci set firewall.{sec}{kind}.dest='*'")
            cmds.append(f"uci set firewall.{sec}{kind}.src_mac={_q(item['mac'])}")
            cmds.append(f"uci set firewall.{sec}{kind}.target='REJECT'")
    for item in plan.get("forwards") or []:
        sec = f"{PREFIX}fwd_{item['id']}"
        cmds.append(f"uci set firewall.{sec}=redirect")
        cmds.append(f"uci set firewall.{sec}.name={_q('HA: ' + item['name'])}")
        cmds.append(f"uci set firewall.{sec}.target='DNAT'")
        cmds.append(f"uci set firewall.{sec}.src='wan'")
        cmds.append(f"uci set firewall.{sec}.dest={_q(_zone_for(plan, item['to_ip']))}")
        cmds.append(
            f"uci set firewall.{sec}.proto={_q({'tcp': 'tcp', 'udp': 'udp', 'both': 'tcp udp'}[item['protocol']])}"
        )
        cmds.append(f"uci set firewall.{sec}.src_dport={_q(item['port'])}")
        cmds.append(f"uci set firewall.{sec}.dest_ip={_q(item['to_ip'])}")
        cmds.append(f"uci set firewall.{sec}.dest_port={_q(item['to_port'])}")
        if not item.get("enabled", True):
            cmds.append(f"uci set firewall.{sec}.enabled='0'")
    return cmds


def openwrt_sync_lines(plan: dict[str, Any], now: datetime) -> list[str]:
    """Replace just the extras on an OpenWrt router, save and reload DHCP and the firewall."""
    cleanup = [
        f"for s in $(uci -q show {cfg} | sed -n 's/^{cfg}\\.\\({PREFIX}{kind}_[A-Za-z0-9_]*\\)=.*/\\1/p'); do uci delete {cfg}.$s; done"
        for cfg, kind in (("dhcp", "res"), ("firewall", "blk"), ("firewall", "fwd"))
    ]
    return [
        *cleanup,
        *openwrt_lines(plan, now),
        "uci commit dhcp",
        "uci commit firewall",
        "/etc/init.d/dnsmasq reload",
        "/etc/init.d/firewall reload",
    ]


# --------------------------------------------------------------------------
# OPNsense
# --------------------------------------------------------------------------


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [r for r in payload["rows"] if isinstance(r, dict)]
    return []


def _enabled(value: Any) -> bool:
    return str(value) in ("1", "True", "true")


def _saved(payload: Any, what: str) -> str:
    if isinstance(payload, dict) and payload.get("result") == "saved":
        return str(payload.get("uuid") or "")
    detail = ""
    if isinstance(payload, dict) and isinstance(payload.get("validations"), dict):
        detail = "; ".join(
            f"{k.split('.')[-1]}: {v}" for k, v in payload["validations"].items()
        )
    raise ExtrasError(
        f"{what}: the router refused it{f' ({detail})' if detail else ''}"
    )


def _mine(row: dict[str, Any], mark: str) -> bool:
    return any(str(row.get(k) or "").startswith(mark) for k in ("description", "descr"))


async def _reload(client: Any, path: str) -> None:
    await client.request("POST", path, {}, timeout=SLOW)


async def _get(client: Any, path: str) -> Any:
    try:
        return await client.get(path)
    except Exception as err:  # noqa: BLE001 - not installed / no access
        if type(err).__name__ == "OPNsenseAuthError":
            raise
        return None


async def opnsense_domain(client: Any) -> str:
    info = await _get(client, "diagnostics/system/system_information")
    name = str((info or {}).get("name") or "")
    return name.split(".", 1)[1] if "." in name else "home.arpa"


async def sync_opnsense(client: Any, plan: dict[str, Any], now: datetime) -> list[str]:
    """Make the router's [HA:…] reservations, DNS names, block rule and port forwards match the plan."""
    done: list[str] = []
    try:
        done += await _sync_reservations(client, plan)
        done += await _sync_blocks(client, plan, now)
        done += await _sync_forwards(client, plan)
    except ExtrasError:
        raise
    except Exception as err:
        if type(err).__name__ == "OPNsenseAuthError":
            raise ExtrasError(
                "the API key may not change this (it needs the DHCP, Unbound DNS and Firewall pages)"
            ) from err
        raise ExtrasError(str(err)) from err
    return done


async def _sync_reservations(client: Any, plan: dict[str, Any]) -> list[str]:
    wanted = plan.get("reservations") or []
    done = []
    kea = await _get(client, "kea/dhcpv4/get")
    dnsmasq = await _get(client, "dnsmasq/settings/get")
    use_kea = _enabled(
        (((kea or {}).get("dhcpv4") or {}).get("general") or {}).get("enabled")
    )
    use_dnsmasq = not use_kea and _enabled(
        ((dnsmasq or {}).get("dnsmasq") or {}).get("enable")
    )
    if wanted and not (use_kea or use_dnsmasq):
        raise ExtrasError(
            "no DHCP server (Kea or Dnsmasq) is enabled on the router, so fixed IPs can't be set"
        )

    if use_kea:
        rows = [
            r
            for r in _rows(await client.post("kea/dhcpv4/search_reservation", SEARCH))
            if _mine(r, FIXED)
        ]
        have = {
            (
                str(r.get("hw_address", "")).lower(),
                str(r.get("ip_address", "")),
                str(r.get("hostname", "")),
            ): r
            for r in rows
        }
        want = {(i["mac"], i["ip"], hostname(i.get("name") or "")): i for i in wanted}
        if have.keys() != want.keys():
            subnets4 = [
                (r.get("uuid"), r.get("subnet"))
                for r in _rows(await client.post("kea/dhcpv4/search_subnet", SEARCH))
            ]
            for key, row in have.items():
                if key not in want:
                    await client.post(f"kea/dhcpv4/del_reservation/{row['uuid']}")
            for key, item in want.items():
                if key in have:
                    continue
                address = ipaddress.IPv4Address(item["ip"])
                subnet = next((u for u, s in subnets4 if _contains(s, address)), None)
                if subnet is None:
                    raise ExtrasError(
                        f"{item['ip']}: no Kea DHCP subnet on the router covers this address"
                    )
                _saved(
                    await client.post(
                        "kea/dhcpv4/add_reservation",
                        {
                            "reservation": {
                                "subnet": subnet,
                                "ip_address": item["ip"],
                                "hw_address": item["mac"],
                                "hostname": key[2],
                                "description": f"{FIXED} {item.get('name') or item['mac']}",
                            }
                        },
                    ),
                    f"fixed IP {item['ip']}",
                )
            await _reload(client, "kea/service/reconfigure")
            done.append(f"{len(want)} fixed IP(s) (Kea)")
    elif use_dnsmasq:
        rows = [
            r
            for r in _rows(await client.post("dnsmasq/settings/search_host", SEARCH))
            if _mine(r, FIXED)
        ]
        have = {
            (
                str(r.get("hwaddr", "")).lower(),
                str(r.get("ip", "")),
                str(r.get("host", "")),
            ): r
            for r in rows
        }
        want = {(i["mac"], i["ip"], hostname(i.get("name") or "")): i for i in wanted}
        if have.keys() != want.keys():
            for key, row in have.items():
                if key not in want:
                    await client.post(f"dnsmasq/settings/del_host/{row['uuid']}")
            for key, item in want.items():
                if key not in have:
                    host = {
                        "ip": item["ip"],
                        "hwaddr": item["mac"],
                        "descr": f"{FIXED} {item.get('name') or item['mac']}",
                    }
                    if key[2]:
                        host["host"] = key[2]
                    _saved(
                        await client.post("dnsmasq/settings/add_host", {"host": host}),
                        f"fixed IP {item['ip']}",
                    )
            await _reload(client, "dnsmasq/service/reconfigure")
            done.append(f"{len(want)} fixed IP(s) (Dnsmasq)")

    # DNS names in Unbound (the usual resolver on OPNsense).
    unbound = await _get(client, "unbound/settings/get")
    if _enabled(
        (((unbound or {}).get("unbound") or {}).get("general") or {}).get("enabled")
    ):
        domain = await opnsense_domain(client)
        rows = [
            r
            for r in _rows(
                await client.post("unbound/settings/search_host_override", SEARCH)
            )
            if _mine(r, FIXED)
        ]
        have = {(str(r.get("hostname", "")), str(r.get("server", ""))): r for r in rows}
        want = {
            (hostname(i.get("name") or ""), i["ip"]): i
            for i in wanted
            if hostname(i.get("name") or "")
        }
        if have.keys() != want.keys():
            for key, row in have.items():
                if key not in want:
                    await client.post(
                        f"unbound/settings/del_host_override/{row['uuid']}"
                    )
            for key, item in want.items():
                if key not in have:
                    _saved(
                        await client.post(
                            "unbound/settings/add_host_override",
                            {
                                "host": {
                                    "enabled": "1",
                                    "hostname": key[0],
                                    "domain": domain,
                                    "rr": "A",
                                    "server": item["ip"],
                                    "description": f"{FIXED} {item.get('name') or item['mac']}",
                                }
                            },
                        ),
                        f"DNS name {key[0]}",
                    )
            await _reload(client, "unbound/service/reconfigure")
            done.append(f"{len(want)} DNS name(s) (.{domain})")
    return done


def _contains(subnet: Any, address: ipaddress.IPv4Address) -> bool:
    try:
        return address in ipaddress.IPv4Network(str(subnet).split(" ")[0], strict=False)
    except ValueError:
        return False


async def _inside_interfaces(client: Any) -> list[str]:
    rows = _rows(await client.post("interfaces/assignment/search_item", SEARCH))
    keys = [str(r.get("identifier") or r.get("uuid")) for r in rows]
    return [
        k
        for k in keys
        if k
        and not k.startswith("wan")
        and _enabled(
            next(
                (
                    r.get("enable", "1")
                    for r in rows
                    if str(r.get("identifier") or r.get("uuid")) == k
                ),
                "1",
            )
        )
    ]


async def _sync_blocks(client: Any, plan: dict[str, Any], now: datetime) -> list[str]:
    macs = sorted({i["mac"] for i in active_blocks(plan, now)})
    aliases = [
        r
        for r in _rows(await client.post("firewall/alias/search_item", SEARCH))
        if r.get("name") == BLOCK_ALIAS
    ]
    rules = [
        r
        for r in _rows(await client.post("firewall/filter/search_rule", SEARCH))
        if _mine(r, BLOCK)
    ]
    alias = aliases[0] if aliases else None
    current = sorted(
        m
        for m in str((alias or {}).get("content") or "").replace(",", "\n").split("\n")
        if m.strip()
    )
    if not macs:
        if not alias and not rules:
            return []
        for row in rules:
            await client.post(f"firewall/filter/del_rule/{row['uuid']}")
        await _reload(client, "firewall/filter/apply")
        if alias:
            await client.post(f"firewall/alias/del_item/{alias['uuid']}")
            await _reload(client, "firewall/alias/reconfigure")
        return ["no device blocked"]
    if alias is None or current != macs:
        item = {
            "alias": {
                "enabled": "1",
                "name": BLOCK_ALIAS,
                "type": "mac",
                "content": "\n".join(macs),
                "description": f"{BLOCK} blocked devices",
            }
        }
        if alias is None:
            _saved(
                await client.post("firewall/alias/add_item", item),
                "blocked devices list",
            )
        else:
            _saved(
                await client.post(f"firewall/alias/set_item/{alias['uuid']}", item),
                "blocked devices list",
            )
        await _reload(client, "firewall/alias/reconfigure")
    if not rules:
        inside = await _inside_interfaces(client)
        rule = {"enabled": "1", "action": "block", "quick": "1", "interface": ",".join(inside), "direction": "in", "ipprotocol": "inet46",
                "protocol": "any", "source_net": BLOCK_ALIAS, "destination_net": "any", "sequence": "1", "description": f"{BLOCK} blocked devices"}  # fmt: skip
        _saved(
            await client.post("firewall/filter/add_rule", {"rule": rule}), "block rule"
        )
    await _reload(client, "firewall/filter/apply")
    return [f"{len(macs)} device(s) blocked"]


async def _sync_forwards(client: Any, plan: dict[str, Any]) -> list[str]:
    wanted = plan.get("forwards") or []
    old = [
        r
        for r in _rows(await client.post("firewall/d_nat/search_rule", SEARCH))
        if _mine(r, FORWARD)
    ]
    if not wanted and not old:
        return []
    for item in wanted:
        rule = {
            "disabled": "0" if item.get("enabled", True) else "1",
            "interface": "wan",
            "ipprotocol": "inet",
            "protocol": PROTOCOLS[item["protocol"]],
            "destination": {"network": "wanip", "port": item["port"]},
            "target": item["to_ip"],
            "local-port": item["to_port"].split("-")[0],
            "pass": "pass",
            "descr": f"{FORWARD} {item['name']}",
        }
        _saved(
            await client.post("firewall/d_nat/add_rule", {"rule": rule}),
            f"port forward {item['name']}",
        )
    for row in old:  # the new ones are in place before the old ones go
        await client.post(f"firewall/d_nat/del_rule/{row['uuid']}")
    await _reload(client, "firewall/d_nat/apply")
    return [f"{len(wanted)} port forward(s)"]
