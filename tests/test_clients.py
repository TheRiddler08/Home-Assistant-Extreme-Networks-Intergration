"""What's known about connected devices: maker, private addresses, names from every source, IPs."""

from __future__ import annotations

import pytest

from custom_components.aerohive_local.parsers import parse_arp_table

from .conftest import HAS_HA, load_fixture


def test_parse_arp_tables() -> None:
    assert parse_arp_table(load_fixture("synthetic_exos_show_iparp.txt")) == {"02:00:00:03:00:cb": "192.168.9.30", "b8:a4:4f:00:d3:de": "192.168.9.41"}
    fastpath = "IP Address       MAC Address        Interface  Type     Age\n192.168.9.5      00-11-22-33-44-55  1/0/3      Dynamic  0h 1m\n"
    assert parse_arp_table(fastpath) == {"00:11:22:33:44:55": "192.168.9.5"}


if HAS_HA:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar, device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local import clients
    from custom_components.aerohive_local.plan import get_plan
    from custom_components.aerohive_local.snapshot import network_snapshot

    from .test_exos_and_backups import exos_entry  # noqa: F401
    from .test_init import FakeClient, fake_client  # noqa: F401
    from .test_openwrt_ha import ow_entry  # noqa: F401

needs_ha = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")


@needs_ha
def test_kind_and_private_address() -> None:
    assert clients.is_private_mac("02:11:22:33:44:55") and not clients.is_private_mac("3c:22:fb:12:34:56")
    assert clients.guess_kind("Pixel-8") == "phone"
    assert clients.guess_kind(None, "Living-room-TV") == "tv"
    assert clients.guess_kind("nas", None, None, None, "Raspberry Pi Foundation") == "smart home"
    assert clients.guess_kind("thing") is None


@needs_ha
async def test_directory_on_openwrt(hass: HomeAssistant, hass_ws_client, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    snap = network_snapshot(hass)
    directory = snap["clients"]
    # Wi-Fi client with a DHCP hostname; a wired neighbour named from the lease; the WAN gateway isn't a client.
    assert directory["02:11:22:33:44:01"]["name"] == "Pixel-8" and directory["02:11:22:33:44:01"]["ip"] == "192.168.1.101"
    assert directory["02:aa:bb:cc:dd:01"]["hostname"] == "nas" and directory["02:aa:bb:cc:dd:01"]["ip"] == "192.168.1.50"
    assert directory["02:11:22:33:44:01"]["private"] is True and directory["02:11:22:33:44:01"]["vendor"] is None
    assert "02:ee:00:00:00:01" not in directory
    assert all("neighbors" not in d for d in snap["devices"])

    # A device another integration knows (same MAC) gives the name, model and area.
    other = MockConfigEntry(domain="cast", title="Cast")
    other.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Office")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, "02:AA:BB:CC:DD:01")},
        name="Office NAS", manufacturer="Synology", model="DS920+", suggested_area="Office",
    )
    info = network_snapshot(hass)["clients"]["02:aa:bb:cc:dd:01"]
    assert info["name"] == "Office NAS" and info["name_source"] == "home_assistant"
    assert info["ha_device"]["model"] == "DS920+" and info["ha_device"]["area"] == area.name

    # A nickname wins; empty removes it. Admin only.
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "aerohive_local/client/rename", "mac": "02:AA:BB:CC:DD:01", "name": "Backup box"})
    assert (await client.receive_json())["success"]
    info = network_snapshot(hass)["clients"]["02:aa:bb:cc:dd:01"]
    assert info["name"] == "Backup box" and info["name_source"] == "nickname"
    await client.send_json({"id": 2, "type": "aerohive_local/client/rename", "mac": "nope", "name": "x"})
    assert not (await client.receive_json())["success"]
    await clients.async_set_nickname(hass, "02:aa:bb:cc:dd:01", "")
    assert network_snapshot(hass)["clients"]["02:aa:bb:cc:dd:01"]["name"] == "Office NAS"

    # A per-device key tied to the MAC names the device and tells its Wi-Fi.
    manager = get_plan(hass)
    manager.plan["wifi"] = [{"id": "home", "ssid": "home", "network": "x", "security": "ppsk"}]
    manager.plan["keys"] = [{"id": "tv", "name": "Lounge TV", "wifi": "home", "mac": "02:11:22:33:44:01"}]
    info = network_snapshot(hass)["clients"]["02:11:22:33:44:01"]
    assert info["key"] == {"name": "Lounge TV", "wifi": "home"}
    assert info["name"] == "Lounge TV" and info["name_source"] == "key" and info["hostname"] == "Pixel-8"


@needs_ha
async def test_wired_ips_from_switch_arp(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = exos_entry.runtime_data
    coordinator.client.responses["show iparp"] = load_fixture("synthetic_exos_show_iparp.txt")
    coordinator._slow_at = None
    await coordinator.async_refresh()
    assert coordinator.data.neighbors["02:00:00:03:00:cb"]["ip"] == "192.168.9.30"
    directory = network_snapshot(hass)["clients"]
    learned_on_ports = {e.mac for e in coordinator.data.fdb if e.port and e.port not in coordinator.data.uplink_ports()}
    assert "b8:a4:4f:00:d3:de" in learned_on_ports
    assert directory["b8:a4:4f:00:d3:de"]["ip"] == "192.168.9.41"
    assert directory["b8:a4:4f:00:d3:de"]["private"] is False
