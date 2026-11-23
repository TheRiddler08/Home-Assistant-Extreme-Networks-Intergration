"""The network setup on an OPNsense router: what is planned and how the API is driven (fake router)."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local import opnsense_setup as s


def _plan(networks: list[dict] | None = None, trunks: list[str] | None = None) -> dict:
    return n.normalize(
        {
            "networks": networks
            if networks is not None
            else [
                {
                    "id": "home",
                    "name": "Home",
                    "vlan": 50,
                    "subnet": "10.40.33.1/24",
                    "reach": ["iot", "lan"],
                },
                {
                    "id": "iot",
                    "name": "IoT",
                    "vlan": 30,
                    "subnet": "10.15.19.1/24",
                    "router_access": True,
                    "lease": "2h",
                },
                {
                    "id": "guest",
                    "name": "Guest",
                    "vlan": 40,
                    "subnet": "10.99.99.1/24",
                    "dhcp": False,
                    "internet": True,
                },
            ],
            "devices": {
                "router": "opn",
                "router_trunks": trunks if trunks is not None else ["br-lan"],
            },
        }
    )


class FakeOPNsense:
    """Just enough of the OPNsense API: VLANs, assignments, Kea/Dnsmasq, filter rules."""

    def __init__(self, dhcp: str | None = "kea") -> None:
        self.ids = itertools.count(1)
        self.calls: list[str] = []
        self.vlans: dict[str, dict] = {}
        self.assignments: dict[str, dict] = {
            "wan": {"descr": "WAN", "if": "igc0", "ipaddr": "dhcp", "enable": "1"},
            "lan": {
                "descr": "LAN",
                "if": "igc1",
                "ipaddr": "192.168.1.1/24",
                "enable": "1",
            },
        }
        self.rules: dict[str, dict] = {
            "own": {"description": "Allow LAN to any", "interface": "lan"}
        }
        self.kea_subnets: dict[str, dict] = {}
        self.kea = {"enabled": "1" if dhcp == "kea" else "0", "interfaces": ["lan"]}
        self.ranges: dict[str, dict] = {}
        self.dnsmasq = {"enable": "1" if dhcp == "dnsmasq" else "0", "interface": []}
        self.fail_on: str | None = None
        self.dead = False

    def _uuid(self) -> str:
        return f"u{next(self.ids)}"

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def post(self, path: str, payload: Any = None) -> Any:
        return await self.request("POST", path, payload or {})

    async def request(
        self, method: str, path: str, payload: Any = None, **kwargs: Any
    ) -> Any:
        await asyncio.sleep(0)
        self.calls.append(path)
        if self.dead:
            raise ConnectionError("gone")
        if self.fail_on and path.startswith(self.fail_on):
            return {"result": "failed", "validations": {"x.field": "not valid"}}
        base, _, item = path.rpartition("/") if path.count("/") > 2 else (path, "", "")
        if path == "interfaces/vlan_settings/search_item":
            return {"rows": [{"uuid": k, **v} for k, v in self.vlans.items()]}
        if path == "interfaces/vlan_settings/add_item":
            uuid = self._uuid()
            self.vlans[uuid] = dict(payload["vlan"])
            return {"result": "saved", "uuid": uuid}
        if base == "interfaces/vlan_settings/del_item":
            self.vlans.pop(item)
            return {"result": "deleted"}
        if path == "interfaces/assignment/search_item":
            return {
                "rows": [
                    {"uuid": k, "identifier": k, **v}
                    for k, v in self.assignments.items()
                ]
            }
        if path == "interfaces/assignment/add_item":
            key = next(
                f"opt{i}"
                for i in itertools.count(1)
                if f"opt{i}" not in self.assignments
            )
            self.assignments[key] = dict(payload["interface"])
            return {"result": "saved", "uuid": key}
        if base == "interfaces/assignment/set_item":
            self.assignments[item] = dict(payload["interface"])
            return {"result": "saved"}
        if base == "interfaces/assignment/del_item":
            self.assignments.pop(item)
            return {"result": "deleted"}
        if path == "kea/dhcpv4/get":
            return {"dhcpv4": {"general": {"enabled": self.kea["enabled"], "interfaces": {
                k: {"value": k.upper(), "selected": int(k in self.kea["interfaces"])} for k in self.assignments}}}}  # fmt: skip
        if path == "kea/dhcpv4/set":
            self.kea["interfaces"] = payload["dhcpv4"]["general"]["interfaces"].split(
                ","
            )
            return {"result": "saved"}
        if path == "kea/dhcpv4/search_subnet":
            return {"rows": [{"uuid": k, **v} for k, v in self.kea_subnets.items()]}
        if path == "kea/dhcpv4/add_subnet":
            uuid = self._uuid()
            self.kea_subnets[uuid] = dict(payload["subnet4"])
            return {"result": "saved", "uuid": uuid}
        if base == "kea/dhcpv4/set_subnet":
            self.kea_subnets[item] = dict(payload["subnet4"])
            return {"result": "saved"}
        if base == "kea/dhcpv4/del_subnet":
            self.kea_subnets.pop(item)
            return {"result": "deleted"}
        if path == "dnsmasq/settings/get":
            return {"dnsmasq": {"enable": self.dnsmasq["enable"], "interface": {
                k: {"value": k, "selected": int(k in self.dnsmasq["interface"])} for k in self.assignments}}}  # fmt: skip
        if path == "dnsmasq/settings/set":
            self.dnsmasq["interface"] = payload["dnsmasq"]["interface"].split(",")
            return {"result": "saved"}
        if path == "dnsmasq/settings/search_range":
            return {"rows": [{"uuid": k, **v} for k, v in self.ranges.items()]}
        if path == "dnsmasq/settings/add_range":
            uuid = self._uuid()
            self.ranges[uuid] = dict(payload["range"])
            return {"result": "saved", "uuid": uuid}
        if base == "dnsmasq/settings/del_range":
            self.ranges.pop(item)
            return {"result": "deleted"}
        if path == "firewall/filter/search_rule":
            return {"rows": [{"uuid": k, **v} for k, v in self.rules.items()]}
        if path == "firewall/filter/add_rule":
            uuid = self._uuid()
            self.rules[uuid] = dict(payload["rule"])
            return {"result": "saved", "uuid": uuid}
        if base == "firewall/filter/del_rule":
            self.rules.pop(item)
            return {"result": "deleted"}
        if path.endswith(("/reconfigure", "/apply")):
            return {"status": "ok"}
        raise AssertionError(f"unexpected {method} {path}")


def run(coro):
    return asyncio.run(coro)


def test_networks_and_rules() -> None:
    nets = s.networks(_plan())
    assert [(x.id, x.vid, x.address, x.subnet, x.pool) for x in nets] == [
        ("home", 50, "10.40.33.1/24", "10.40.33.0/24", ("10.40.33.50", "10.40.33.199")),
        ("iot", 30, "10.15.19.1/24", "10.15.19.0/24", ("10.15.19.50", "10.15.19.199")),
        ("guest", 40, "10.99.99.1/24", "10.99.99.0/24", None),
    ]
    assert (
        nets[1].lease == 7200
        and s.lease_seconds("1d") == 86400
        and s.lease_seconds("bad") == 43200
    )
    assert s.vlan_device(30) == "vlan0.30" and s.assignment_name("Kid-s") == "hn_kid_s"

    rules = s.firewall_rules(
        nets, {"home": "opt1", "iot": "opt2", "guest": "opt3", "lan": "lan"}
    )
    home = [
        (r["action"], r["destination_net"], r["destination_port"])
        for r in rules
        if r["interface"] == "opt1"
    ]
    assert home == [
        ("pass", "any", "67"), ("pass", "(self)", "53"), ("block", "(self)", ""), ("pass", "opt2", ""), ("pass", "lan", ""),
        ("block", "10.0.0.0/8", ""), ("block", "172.16.0.0/12", ""), ("block", "192.168.0.0/16", ""), ("pass", "any", ""),
    ]  # fmt: skip
    assert ("pass", "(self)") in [
        (r["action"], r["destination_net"]) for r in rules if r["interface"] == "opt2"
    ]
    assert all(
        r["description"].startswith("[HA] ") and r["source_net"] == r["interface"]
        for r in rules
    )

    lines = s.describe(_plan())
    assert lines[0] == "VLAN vlan0.50 on the LAN port (tag 50)"
    assert "interface hn_iot on vlan0.30: static 10.15.19.1/24" in lines
    assert s.describe(_plan(trunks=["igc2"]))[0].endswith("on igc2 (tag 50)")


def test_apply_creates_everything_on_kea() -> None:
    router = FakeOPNsense("kea")
    result = run(s.async_apply(router, _plan(), ["br-lan"]))
    assert result.done[0] == "trunk igc1"  # the default trunk means the LAN port
    assert sorted((v["if"], v["tag"], v["vlanif"]) for v in router.vlans.values()) == [
        ("igc1", "30", "vlan0.30"), ("igc1", "40", "vlan0.40"), ("igc1", "50", "vlan0.50")
    ]  # fmt: skip
    ours = {k: v for k, v in router.assignments.items() if k.startswith("opt")}
    assert {
        k: (v["descr"], v["if"], v["ipaddr"], v["type4"]) for k, v in ours.items()
    } == {
        "opt1": ("hn_home", "vlan0.50", "10.40.33.1/24", "staticv4"),
        "opt2": ("hn_iot", "vlan0.30", "10.15.19.1/24", "staticv4"),
        "opt3": ("hn_guest", "vlan0.40", "10.99.99.1/24", "staticv4"),
    }
    assert sorted((x["interface"], x["pools"]) for x in router.kea_subnets.values()) == [
        ("opt1", "10.40.33.50-10.40.33.199"), ("opt2", "10.15.19.50-10.15.19.199")
    ]  # fmt: skip
    assert router.kea["interfaces"] == ["lan", "opt1", "opt2"]
    assert (
        router.rules["own"]["description"] == "Allow LAN to any"
    )  # your own rules are left alone
    assert len(router.rules) == 1 + len(
        s.firewall_rules(
            s.networks(_plan()), {"home": "1", "iot": "2", "guest": "3", "lan": "lan"}
        )
    )
    # Reconfigure after each kind of change, the filter applied once.
    for path in (
        "interfaces/vlan_settings/reconfigure",
        "interfaces/assignment/reconfigure",
        "kea/service/reconfigure",
    ):
        assert path in router.calls
    assert router.calls.count("firewall/filter/apply") == 1

    # Applying the same setup again adds nothing new: same VLANs and interfaces, rules replaced.
    calls = len(router.calls)
    run(s.async_apply(router, _plan(), ["br-lan"]))
    assert (
        len(router.vlans) == 3
        and len([k for k in router.assignments if k.startswith("opt")]) == 3
    )
    assert "interfaces/vlan_settings/add_item" not in router.calls[calls:]
    assert "interfaces/assignment/add_item" not in router.calls[calls:]
    assert len(router.kea_subnets) == 2

    # A network removed from the setup: its interface, VLAN, DHCP and rules go; LAN and WAN stay.
    plan = _plan([{"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24"}])
    run(s.async_apply(router, plan, ["br-lan"]))
    assert [v["vlanif"] for v in router.vlans.values()] == ["vlan0.30"]
    assert sorted(router.assignments) == ["lan", "opt2", "wan"]
    assert [x["interface"] for x in router.kea_subnets.values()] == ["opt2"]
    assert router.kea["interfaces"] == ["lan", "opt2"]
    assert {r["interface"] for k, r in router.rules.items() if k != "own"} == {"opt2"}

    # A changed address is updated in place.
    plan = _plan([{"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.20.1/24"}])
    run(s.async_apply(router, plan, ["br-lan"]))
    assert router.assignments["opt2"]["ipaddr"] == "10.15.20.1/24"


def test_apply_on_dnsmasq_and_own_trunk() -> None:
    router = FakeOPNsense("dnsmasq")
    router.dnsmasq["interface"] = ["lan"]
    run(s.async_apply(router, _plan(), ["igc2"]))
    assert {v["if"] for v in router.vlans.values()} == {"igc2"}
    assert sorted((r["interface"], r["start_addr"], r["end_addr"], r["lease_time"]) for r in router.ranges.values()) == [
        ("opt1", "10.40.33.50", "10.40.33.199", "43200"), ("opt2", "10.15.19.50", "10.15.19.199", "7200")
    ]  # fmt: skip
    assert "kea/dhcpv4/add_subnet" not in router.calls
    assert router.dnsmasq["interface"] == ["lan", "opt1", "opt2"]


def test_no_dhcp_server_is_a_warning() -> None:
    router = FakeOPNsense(None)
    result = run(s.async_apply(router, _plan(), None))
    assert any("No DHCP server" in w for w in result.warnings)
    assert router.vlans and router.rules


def test_existing_vlan_is_used_and_foreign_interface_refused() -> None:
    router = FakeOPNsense()
    router.vlans["mine"] = {
        "if": "igc1",
        "tag": "30",
        "vlanif": "vlan01",
        "descr": "my IoT",
    }
    run(
        s.async_apply(
            router,
            _plan(
                [{"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24"}]
            ),
            None,
        )
    )
    assert len(router.vlans) == 1 and router.assignments["opt1"]["if"] == "vlan01"
    # Removing it from the setup doesn't delete a VLAN you made.
    run(s.async_apply(router, _plan([]), None))
    assert "mine" in router.vlans and "opt1" not in router.assignments

    router = FakeOPNsense()
    router.vlans["mine"] = {
        "if": "igc1 (LAN)",
        "tag": "30",
        "vlanif": "vlan0.30",
        "descr": "my IoT",
    }
    router.assignments["opt1"] = {
        "descr": "IOT",
        "if": "vlan0.30",
        "ipaddr": "10.15.19.1/24",
        "enable": "1",
    }
    with pytest.raises(s.SetupError, match="already in use as interface IOT"):
        run(
            s.async_apply(
                router,
                _plan(
                    [
                        {
                            "id": "iot",
                            "name": "IoT",
                            "vlan": 30,
                            "subnet": "10.15.19.1/24",
                        }
                    ]
                ),
                None,
            )
        )
    assert router.assignments["opt1"]["descr"] == "IOT"


def test_failure_undoes_what_was_added() -> None:
    router = FakeOPNsense()
    router.fail_on = "firewall/filter/add_rule"
    with pytest.raises(
        s.SetupError, match=r"the router refused it \(field: not valid\)"
    ):
        run(s.async_apply(router, _plan(), None))
    assert (
        router.vlans == {}
        and sorted(router.assignments) == ["lan", "wan"]
        and router.kea_subnets == {}
    )
    assert list(router.rules) == ["own"]
    assert router.calls[-1].endswith("reconfigure")  # undone changes applied again

    router = FakeOPNsense()
    router.assignments["lan"]["if"] = ""
    with pytest.raises(s.SetupError, match="LAN port"):
        run(s.async_apply(router, _plan(), None))


def test_auth_error_explains_rights() -> None:
    class AuthRouter(FakeOPNsense):
        async def request(self, method, path, payload=None, **kwargs):
            if path == "interfaces/vlan_settings/add_item":
                raise type("OPNsenseAuthError", (Exception,), {})("HTTP 403")
            return await super().request(method, path, payload, **kwargs)

    with pytest.raises(s.SetupError, match="API key may not change this"):
        run(s.async_apply(AuthRouter(), _plan(), None))
