"""Port speed and duplex: commands, reading the configured setting, the action on each kind of switch."""

from __future__ import annotations

import pytest

from custom_components.aerohive_local import commands
from custom_components.aerohive_local.parsers import parse_exos_port_speed_cfg, parse_running_config_summary

from .conftest import HAS_HA


def test_speed_commands() -> None:
    assert commands.validate_speed("auto", "half") == ("auto", "auto")
    assert commands.validate_speed(100, None) == ("100", "full")
    assert commands.validate_speed("1G", "auto") == ("1000", "full")
    with pytest.raises(commands.InvalidArgument):
        commands.validate_speed("1000", "half")
    with pytest.raises(commands.InvalidArgument):
        commands.validate_speed("2500", None)
    assert commands.exos_port_speed("5", "auto", None) == "configure ports 5 auto on"
    assert commands.exos_port_speed("5", "100", "half") == "configure ports 5 auto off speed 100 duplex half"
    assert commands.exos_port_speed("1:5", "1000", None) == "configure ports 1:5 auto on speed 1000 duplex full"
    assert commands.exos_port_speed("49", "10000", None) == "configure ports 49 auto off speed 10000 duplex full"
    assert commands.switch_port_speed("1/0/3", "10", "half") == ["configure", "interface 1/0/3", "speed 10 half-duplex", "exit", "exit"]
    assert commands.switch_port_speed("1/0/3", "auto", None)[2] == "auto-negotiate"
    with pytest.raises(commands.InvalidArgument):
        commands.switch_port_speed("1/0/3", "1000", None)
    assert commands.port_speed("eth1", "100", None) == ["interface eth1 speed 100", "interface eth1 duplex full"]
    with pytest.raises(commands.InvalidArgument):
        commands.exos_port_speed("5; reboot", "auto", None)


def test_read_configured_speed() -> None:
    text = (
        "Port     Virtual    Port  Link  Auto   Speed      Duplex   Flow  Load   Media\n"
        "1        VR-Default  E      A    ON  AUTO  1000 AUTO FULL               UTP\n"
        "2        VR-Default  E      A    OFF 100   100  HALF HALF               UTP\n"
        "3        VR-Default  E      R    OFF 10         FULL                    UTP\n"
        "4        VR-Default  E      A    ON  1000  1000 FULL FULL               UTP\n"
    )
    assert parse_exos_port_speed_cfg(text) == {"1": ("auto", "auto"), "2": ("100", "half"), "3": ("10", "full"), "4": ("1000", "full")}
    config = "interface 1/0/2\nspeed 100 full-duplex\nexit\ninterface 1/0/4\nauto-negotiate\nexit\ninterface eth2 speed 10\n"
    assert parse_running_config_summary(config).port_speeds == {"1/0/2": ("100", "full"), "1/0/4": ("auto", "auto"), "eth2": ("10", "auto")}


if HAS_HA:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local.const import DOMAIN
    from custom_components.aerohive_local.snapshot import device_snapshot

    from .test_discovery_and_switch import switch_entry  # noqa: F401
    from .test_exos_and_backups import exos_entry  # noqa: F401
    from .test_init import FakeClient, entry, fake_client  # noqa: F401

    def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
        return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0].id

needs_ha = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")


@needs_ha
async def test_exos_port_speed(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = exos_entry.runtime_data
    port = next(p for p in coordinator.data.ports() if p not in coordinator.data.uplink_ports() and coordinator.data.interfaces[p].extra.get("media") != "sfp")
    snap = device_snapshot(hass, coordinator)
    row = next(p for p in snap["ports"] if p["name"] == port)
    assert row["speed_setting"] == {"speed": "auto", "duplex": "auto"} and row["speed_options"] == ["auto", "10", "100", "1000"]
    assert snap["features"]["port_speed"] is True
    client = FakeClient.instances[-1]
    await hass.services.async_call(DOMAIN, "set_port_speed", {"device_id": _device_id(hass, exos_entry), "port": port, "speed": "100", "duplex": "full"}, blocking=True)
    assert f"configure ports {port} auto off speed 100 duplex full" in client.commands
    assert coordinator.port_speed_setting(port) == ("100", "full")
    with pytest.raises(HomeAssistantError, match="can be set to"):
        await coordinator.async_set_port_speed(port, "10000")


@needs_ha
async def test_sr_port_speed_and_uplink(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = switch_entry.runtime_data
    client = FakeClient.instances[-1]
    await coordinator.async_set_port_speed("1/0/4", "10", "half")
    start = client.commands.index("speed 10 half-duplex")
    assert client.commands[start - 2:start + 3] == ["configure", "interface 1/0/4", "speed 10 half-duplex", "exit", "exit"]
    assert coordinator.port_speed_setting("1/0/4") == ("10", "half")
    uplink = next(iter(coordinator.data.uplink_ports()), None)
    if uplink:
        with pytest.raises(HomeAssistantError, match="uplink"):
            await coordinator.async_set_port_speed(uplink, "100")
        await coordinator.async_set_port_speed(uplink, "auto")  # back to auto is always allowed


@needs_ha
async def test_hiveos_port_speed(hass: HomeAssistant, entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = entry.runtime_data
    port = next((p for p in coordinator.data.interfaces if p.startswith("eth") and p not in coordinator.data.uplink_ports()), None)
    if port is None:
        pytest.skip("fixture AP has no free Ethernet port")
    await coordinator.async_set_port_speed(port, "100")
    assert f"interface {port} speed 100" in FakeClient.instances[-1].commands
