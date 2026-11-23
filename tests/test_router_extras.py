"""Router extras: fixed IPs, blocked devices and port forwards on OpenWrt (uci) and OPNsense (API)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local import router_extras as x

from .test_netplan_shell import router  # noqa: F401 - fixture
from .test_opnsense_setup import FakeOPNsense

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _plan(**extras) -> dict:
    return n.normalize(
        {
            "networks": [
                {"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24"}
            ],
            "devices": {"router": "r"},
            **extras,
        }
    )


def test_checks() -> None:
    plan = _plan()
    assert x.check_reservation(
        plan, "AA-BB-CC-DD-EE-01", "10.15.19.20", " Pi-hole "
    ) == {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.15.19.20", "name": "Pi-hole"}
    assert x.normalize_mac("aabbccddee02") == "aa:bb:cc:dd:ee:02"
    for mac, ip, error in (
        ("nope", "10.15.19.20", "isn't a MAC"),
        ("aa:bb:cc:dd:ee:01", "8.8.8.8", "private"),
        ("aa:bb:cc:dd:ee:01", "10.15.19.1", "the router"),
        ("aa:bb:cc:dd:ee:01", "10.15.19.255", "broadcast"),
        ("aa:bb:cc:dd:ee:01", "10.15.19.300", "isn't an IPv4"),
    ):
        with pytest.raises(x.ExtrasError, match=error):
            x.check_reservation(plan, mac, ip, "")
    taken = _plan(
        reservations=[{"mac": "aa:bb:cc:dd:ee:09", "ip": "10.15.19.20", "name": "TV"}]
    )
    with pytest.raises(x.ExtrasError, match="already fixed for TV"):
        x.check_reservation(taken, "aa:bb:cc:dd:ee:01", "10.15.19.20", "")
    assert x.hostname("Living room TV!") == "living-room-tv" and x.hostname("") == ""

    first = x.check_forward(
        {
            "name": "Web server",
            "protocol": "tcp",
            "port": "443",
            "to_ip": "10.15.19.20",
        },
        [],
    )
    assert first == {
        "id": "web_server",
        "name": "Web server",
        "protocol": "tcp",
        "port": "443",
        "to_ip": "10.15.19.20",
        "to_port": "443",
        "enabled": True,
    }
    assert (
        x.check_forward(
            {"name": "Game", "protocol": "udp", "port": "443", "to_ip": "10.15.19.21"},
            [first],
        )["protocol"]
        == "udp"
    )
    for item, error in (
        (
            {
                "name": "Again",
                "protocol": "both",
                "port": "443",
                "to_ip": "10.15.19.21",
            },
            "already forwarded by Web server",
        ),
        (
            {"name": "Bad", "protocol": "tcp", "port": "70000", "to_ip": "10.15.19.21"},
            "1 to 65535",
        ),
        (
            {"name": "Bad", "protocol": "tcp", "port": "80", "to_ip": "1.1.1.1"},
            "private",
        ),
        (
            {
                "name": "Bad",
                "protocol": "tcp",
                "port": "8000-8010",
                "to_port": "80",
                "to_ip": "10.15.19.21",
            },
            "range to a range",
        ),
        ({"name": "", "protocol": "tcp", "port": "80", "to_ip": "10.15.19.21"}, "name"),
    ):
        with pytest.raises(x.ExtrasError, match=error):
            x.check_forward(item, [first])

    blocked = [{"mac": "aa:bb:cc:dd:ee:01", "until": None}, {"mac": "aa:bb:cc:dd:ee:02", "until": (NOW - timedelta(minutes=1)).isoformat()},
               {"mac": "aa:bb:cc:dd:ee:03", "until": (NOW + timedelta(minutes=5)).isoformat()}]  # fmt: skip
    assert [b["mac"][-2:] for b in x.active_blocks({"blocked": blocked}, NOW)] == [
        "01",
        "03",
    ]


def test_normalize_keeps_extras_and_setup_apply_includes_them() -> None:
    plan = _plan(
        reservations=[
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.15.19.20", "name": "Pi-hole"}
        ],
        forwards=[
            {
                "id": "web",
                "name": "Web",
                "protocol": "tcp",
                "port": "443",
                "to_ip": "10.15.19.20",
                "to_port": "443",
            }
        ],
    )
    assert plan["reservations"][0]["ip"] == "10.15.19.20" and plan["blocked"] == []
    out = n.openwrt_lines(plan, lambda ref: None, is_router=True, radios={}, now=NOW)
    assert "uci set dhcp.hn_res_aabbccddee01=host" in out.commands
    assert (
        "uci set firewall.hn_fwd_web.dest=hn30" in out.commands
    )  # the zone of the device's network
    ap = n.openwrt_lines(plan, lambda ref: None, is_router=False, radios={}, now=NOW)
    assert not any("hn_res_" in c for c in ap.commands)  # only on the router


def test_openwrt_sync_in_a_shell(router) -> None:  # noqa: F811
    plan = _plan(
        reservations=[
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.15.19.20", "name": "Kid's tablet"}
        ],
        blocked=[{"mac": "aa:bb:cc:dd:ee:02", "name": "Game console", "until": None}],
        forwards=[
            {
                "id": "web",
                "name": "Web $(x)",
                "protocol": "both",
                "port": "443",
                "to_ip": "192.168.1.5",
                "to_port": "8443",
                "enabled": False,
            }
        ],
    )
    store = json.loads(router.store.read_text())
    store["dhcp"]["hn_res_old"] = {".type": "host"}
    store["dhcp"]["lan"] = {".type": "dhcp"}
    store["firewall"]["hn_zone30"] = {".type": "zone"}
    router.store.write_text(json.dumps(store))
    for chunk in n.chunks(x.openwrt_sync_lines(plan, NOW)):
        result = router(f"{chunk} || echo 'FAILED'")
        assert "FAILED" not in result.stdout, (result.stdout, result.stderr)
    store = json.loads(router.store.read_text())
    assert "hn_res_old" not in store["dhcp"] and "lan" in store["dhcp"]
    assert "hn_zone30" in store["firewall"]  # a setup section, left alone
    assert store["dhcp"]["hn_res_aabbccddee01"] == {
        ".type": "host",
        "mac": "aa:bb:cc:dd:ee:01",
        "ip": "10.15.19.20",
        "name": "kid-s-tablet",
        "dns": "1",
    }
    assert store["firewall"]["hn_blk_aabbccddee02fwd"]["src_mac"] == "aa:bb:cc:dd:ee:02"
    assert store["firewall"]["hn_blk_aabbccddee02in"]["target"] == "REJECT"
    fwd = store["firewall"]["hn_fwd_web"]
    assert (
        fwd["name"],
        fwd["proto"],
        fwd["dest"],
        fwd["dest_port"],
        fwd["enabled"],
    ) == ("HA: Web $(x)", "tcp udp", "lan", "8443", "0")
    log = (router.root / "calls.log").read_text()
    assert "dnsmasq reload" in log and "firewall reload" in log


class ExtrasRouter(FakeOPNsense):
    def __init__(self, dhcp: str | None = "kea", unbound: bool = True) -> None:
        super().__init__(dhcp)
        self.kea_subnets = {
            "lansub": {"subnet": "192.168.1.0/24", "description": "LAN"},
            "iotsub": {"subnet": "10.15.19.0/24", "description": "[HA] IoT"},
        }
        self.reservations: dict[str, dict] = {
            "own": {
                "hw_address": "aa:aa:aa:aa:aa:aa",
                "ip_address": "192.168.1.9",
                "description": "printer",
            }
        }
        self.hosts: dict[str, dict] = {}
        self.unbound = unbound
        self.overrides: dict[str, dict] = {}
        self.aliases: dict[str, dict] = {}
        self.dnat: dict[str, dict] = {
            "own": {"descr": "my NAS", "target": "192.168.1.4"}
        }

    async def request(self, method, path, payload=None, **kwargs):  # noqa: C901, PLR0911
        if self.fail_on and path.startswith(self.fail_on):
            return {"result": "failed", "validations": {"rule.target": "not valid"}}
        base, _, item = path.rpartition("/") if path.count("/") > 2 else (path, "", "")
        tables = {
            "kea/dhcpv4": ("reservation", self.reservations, "reservation"),
            "dnsmasq/settings": ("host", self.hosts, "host"),
            "unbound/settings": ("host_override", self.overrides, "host"),
            "firewall/alias": ("item", self.aliases, "alias"),
            "firewall/d_nat": ("rule", self.dnat, "rule"),
        }
        for prefix, (noun, table, key) in tables.items():
            if path == f"{prefix}/search_{noun}":
                self.calls.append(path)
                return {"rows": [{"uuid": k, **v} for k, v in table.items()]}
            if path == f"{prefix}/add_{noun}":
                self.calls.append(path)
                uuid = self._uuid()
                table[uuid] = dict(payload[key])
                return {"result": "saved", "uuid": uuid}
            if base == f"{prefix}/set_{noun}":
                self.calls.append(path)
                table[item] = dict(payload[key])
                return {"result": "saved"}
            if base == f"{prefix}/del_{noun}":
                self.calls.append(path)
                table.pop(item)
                return {"result": "deleted"}
        if path == "unbound/settings/get":
            return {"unbound": {"general": {"enabled": "1" if self.unbound else "0"}}}
        if path == "diagnostics/system/system_information":
            return {"name": "opnsense.home.example"}
        return await super().request(method, path, payload, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_opnsense_sync_on_kea_with_unbound() -> None:
    router = ExtrasRouter("kea")
    router.assignments["opt1"] = {
        "descr": "hn_iot",
        "if": "vlan0.30",
        "ipaddr": "10.15.19.1/24",
        "enable": "1",
    }
    plan = _plan(
        reservations=[
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.15.19.20", "name": "Pi-hole"}
        ],
        blocked=[
            {"mac": "aa:bb:cc:dd:ee:02", "name": "Console", "until": None},
            {
                "mac": "aa:bb:cc:dd:ee:03",
                "until": (NOW - timedelta(minutes=1)).isoformat(),
            },
        ],
        forwards=[
            {
                "id": "web",
                "name": "Web",
                "protocol": "tcp",
                "port": "443",
                "to_ip": "10.15.19.20",
                "to_port": "8443",
                "enabled": True,
            }
        ],
    )
    done = run(x.sync_opnsense(router, plan, NOW))
    assert done == [
        "1 fixed IP(s) (Kea)",
        "1 DNS name(s) (.home.example)",
        "1 device(s) blocked",
        "1 port forward(s)",
    ]
    ours = [r for k, r in router.reservations.items() if k != "own"]
    assert ours == [
        {
            "subnet": "iotsub",
            "ip_address": "10.15.19.20",
            "hw_address": "aa:bb:cc:dd:ee:01",
            "hostname": "pi-hole",
            "description": "[HA:fixed] Pi-hole",
        }
    ]
    assert [
        (o["hostname"], o["domain"], o["server"]) for o in router.overrides.values()
    ] == [("pi-hole", "home.example", "10.15.19.20")]
    alias = next(iter(router.aliases.values()))
    assert (alias["name"], alias["type"], alias["content"]) == (
        "HA_blocked",
        "mac",
        "aa:bb:cc:dd:ee:02",
    )  # the ended pause isn't in it
    rule = next(
        r
        for r in router.rules.values()
        if r.get("description", "").startswith("[HA:block]")
    )
    assert (
        rule["interface"] == "lan,opt1"
        and rule["source_net"] == "HA_blocked"
        and rule["sequence"] == "1"
        and rule["action"] == "block"
    )
    nat = next(r for k, r in router.dnat.items() if k != "own")
    assert (
        nat["destination"] == {"network": "wanip", "port": "443"}
        and nat["target"] == "10.15.19.20"
        and nat["local-port"] == "8443"
        and nat["interface"] == "wan"
    )
    assert (
        router.reservations["own"]["description"] == "printer"
        and router.dnat["own"]["descr"] == "my NAS"
    )  # yours stay
    for path in (
        "kea/service/reconfigure",
        "unbound/service/reconfigure",
        "firewall/alias/reconfigure",
        "firewall/filter/apply",
        "firewall/d_nat/apply",
    ):
        assert path in router.calls

    # Again with nothing changed: no reconfigure of DHCP or DNS, the block list stays.
    calls = len(router.calls)
    run(x.sync_opnsense(router, plan, NOW))
    assert (
        "kea/service/reconfigure" not in router.calls[calls:]
        and "firewall/alias/add_item" not in router.calls[calls:]
    )

    # Everything removed: only ours goes.
    run(x.sync_opnsense(router, _plan(), NOW))
    assert (
        list(router.reservations) == ["own"]
        and router.overrides == {}
        and router.aliases == {}
        and list(router.dnat) == ["own"]
    )
    assert not any(
        r.get("description", "").startswith("[HA:block]") for r in router.rules.values()
    )


def test_opnsense_sync_on_dnsmasq_and_errors() -> None:
    router = ExtrasRouter("dnsmasq", unbound=False)
    plan = _plan(
        reservations=[{"mac": "aa:bb:cc:dd:ee:01", "ip": "10.15.19.20", "name": ""}]
    )
    assert run(x.sync_opnsense(router, plan, NOW)) == ["1 fixed IP(s) (Dnsmasq)"]
    assert list(router.hosts.values()) == [
        {
            "ip": "10.15.19.20",
            "hwaddr": "aa:bb:cc:dd:ee:01",
            "descr": "[HA:fixed] aa:bb:cc:dd:ee:01",
        }
    ]

    with pytest.raises(x.ExtrasError, match="no Kea DHCP subnet"):
        run(
            x.sync_opnsense(
                ExtrasRouter("kea"),
                _plan(
                    reservations=[
                        {"mac": "aa:bb:cc:dd:ee:01", "ip": "172.16.0.5", "name": ""}
                    ]
                ),
                NOW,
            )
        )
    with pytest.raises(x.ExtrasError, match="no DHCP server"):
        run(x.sync_opnsense(ExtrasRouter(None), plan, NOW))
    refusing = ExtrasRouter("kea")
    refusing.fail_on = "firewall/d_nat/add_rule"
    with pytest.raises(x.ExtrasError, match="port forward Web: the router refused it"):
        run(
            x.sync_opnsense(
                refusing,
                _plan(
                    forwards=[
                        {
                            "id": "w",
                            "name": "Web",
                            "protocol": "tcp",
                            "port": "80",
                            "to_ip": "10.15.19.2",
                            "to_port": "80",
                        }
                    ]
                ),
                NOW,
            )
        )
    assert list(refusing.dnat) == ["own"]
