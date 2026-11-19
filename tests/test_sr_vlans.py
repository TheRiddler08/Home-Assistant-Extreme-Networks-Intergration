"""SR switches in the network setup: reading VLANs and the (assumed) FASTPATH VLAN commands."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local.parsers import parse_fastpath_vlans

from .conftest import HAS_HA, load_fixture

PORTS = [f"1/0/{i}" for i in range(1, 11)]


def _plan(entry_id: str = "sw") -> dict:
    return n.normalize({
        "networks": [
            {"id": "home", "name": "Home", "vlan": 50, "subnet": "10.40.33.1/24"},
            {"id": "iot", "name": "IoT / Smart home", "vlan": 30, "subnet": "10.15.19.1/24", "switch_ports": {entry_id: "1-2"}},
        ],
        "devices": {"router": None, "switch_trunks": {entry_id: "9-10"}},
    })


def test_parse_fastpath_vlans() -> None:
    vlans = parse_fastpath_vlans(load_fixture("assumed_sr2208p_show_vlan_brief.txt"))
    assert {name: v.vid for name, v in vlans.items()} == {"default": 1, "home": 50}
    loose = parse_fastpath_vlans("VLAN  Name\n----  ----\n1     default\n20    Servers\n")
    assert {v.vid for v in loose.values()} == {1, 20}


def test_expand_ports() -> None:
    assert n.expand_ports("1-3,9", PORTS) == ["1/0/1", "1/0/2", "1/0/3", "1/0/9"]
    assert n.expand_ports("1/0/4-1/0/5, 1/0/5", PORTS) == ["1/0/4", "1/0/5"]
    with pytest.raises(ValueError, match="doesn't exist"):
        n.expand_ports("11", PORTS)
    with pytest.raises(ValueError, match="invalid port range"):
        n.expand_ports("5-2", PORTS)


def test_sr_commands() -> None:
    out = n.sr_commands(_plan(), entry_id="sw", existing={"default": 1, "home": 50, "old": 99}, ports=PORTS, managed=["99"])
    assert out.errors == []
    assert out.commands == [
        "vlan database",
        "vlan 30",
        'vlan name 30 "IoT Smart home"',
        "vlan name 50 Home",
        "no vlan 99",
        "exit",
        "configure",
        "interface 1/0/9", "vlan participation include 30", "vlan tagging 30", "vlan participation include 50", "vlan tagging 50", "exit",
        "interface 1/0/10", "vlan participation include 30", "vlan tagging 30", "vlan participation include 50", "vlan tagging 50", "exit",
        "interface 1/0/1", "vlan participation include 30", "no vlan tagging 30", "vlan pvid 30", "exit",
        "interface 1/0/2", "vlan participation include 30", "no vlan tagging 30", "vlan pvid 30", "exit",
        "exit",
    ]
    assert out.managed == {"vlans": ["30"]}  # 50 already existed: never deleted later
    assert any("assumed" in w for w in out.warnings)
    clash = _plan()
    clash["networks"][0]["switch_ports"] = {"sw": "2-3"}
    assert "two networks" in n.sr_commands(clash, entry_id="sw", existing={}, ports=PORTS, managed=[]).errors[0]
    trunk_clash = _plan()
    trunk_clash["networks"][1]["switch_ports"] = {"sw": "9"}
    assert "both a trunk" in n.sr_commands(trunk_clash, entry_id="sw", existing={}, ports=PORTS, managed=[]).errors[0]


if HAS_HA:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local.plan import get_plan

    from . import test_discovery_and_switch as sw_tests
    from .test_discovery_and_switch import switch_entry  # noqa: F401
    from .test_init import FakeClient, fake_client  # noqa: F401


@pytest.fixture
def sr_vlans():
    original = sw_tests._switch_responses

    def responses() -> dict[str, str]:
        return {**original(), "show vlan brief": load_fixture("assumed_sr2208p_show_vlan_brief.txt")}

    with patch.object(sw_tests, "_switch_responses", responses):
        yield


@pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")
async def test_sr_switch_in_the_setup(hass: HomeAssistant, sr_vlans, switch_entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = switch_entry.runtime_data
    assert {v.vid for v in coordinator.data.vlans.values()} == {1, 50}
    manager = get_plan(hass)
    await manager.async_save_plan(_plan(switch_entry.entry_id), {})
    preview = manager.preview()
    device = next(d for d in preview["devices"] if d["entry_id"] == switch_entry.entry_id)
    assert device["role"] == "switch" and device["errors"] == [] and "vlan database" in device["commands"]
    assert any("assumed" in w for w in device["warnings"])
    client = FakeClient.instances[-1]
    client.commands.clear()
    results = await manager.async_apply()
    assert results[0]["ok"], results
    sent = client.commands
    start = sent.index("vlan database")
    assert sent[start:start + 4] == ["vlan database", "vlan 30", 'vlan name 30 "IoT Smart home"', "vlan name 50 Home"]
    assert "interface 1/0/9" in sent and "vlan pvid 30" in sent and "write memory" in sent
    assert manager.managed[switch_entry.entry_id]["vlans"] == ["30"]
