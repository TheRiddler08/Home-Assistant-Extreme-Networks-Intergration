"""Tests for discovery (DHCP + network scan), SSID management and SR2208P switch support."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from .conftest import HAS_HA, load_fixture

if not HAS_HA:
    pytest.skip("Home Assistant test dependencies not installed", allow_module_level=True)

from homeassistant import config_entries  # noqa: E402
from homeassistant.const import CONF_HOST  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.aerohive_local.const import (  # noqa: E402
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_CLIENTS,
    DOMAIN,
)
from custom_components.aerohive_local.discovery import ScanResult  # noqa: E402

from .test_init import MAC, USER_INPUT, FakeClient, _client, _entity_id, entry, fake_client  # noqa: E402, F401

CREDS = {k: v for k, v in USER_INPUT.items() if k != CONF_HOST}
SWITCH_MAC = "e0:1c:41:00:10:00"


# --------------------------------------------------------------------------
# DHCP discovery
# --------------------------------------------------------------------------


def _dhcp(ip: str = "192.168.1.10", mac: str = "bcf3107c0680", hostname: str = "ah-7c0680") -> DhcpServiceInfo:
    return DhcpServiceInfo(ip=ip, macaddress=mac, hostname=hostname)


async def test_dhcp_discovery_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"]["host"] == "192.168.1.10"
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC
    assert result["data"][CONF_HOST] == "192.168.1.10"
    assert FakeClient.instances[-1].host == "192.168.1.10"


async def test_dhcp_discovery_updates_ip_of_configured_device(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=USER_INPUT, title="AH-7c0680")
    existing.add_to_hass(hass)
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp(ip="192.168.1.77")
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert existing.data[CONF_HOST] == "192.168.1.77"


# --------------------------------------------------------------------------
# Network scan
# --------------------------------------------------------------------------


async def _scan_flow(hass: HomeAssistant, found: list[ScanResult]) -> dict[str, Any]:
    async def fake_scan(targets: list[str], **kwargs: Any) -> list[ScanResult]:
        await asyncio.sleep(0)
        return found

    adapters = [{"enabled": True, "ipv4": [{"address": "192.168.1.5", "network_prefix": 24}]}]
    with (
        patch("custom_components.aerohive_local.config_flow.async_scan", fake_scan),
        patch(
            "custom_components.aerohive_local.config_flow.network.async_get_adapters",
            return_value=adapters,
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "scan"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_scan_lists_devices_and_creates_entry(hass: HomeAssistant) -> None:
    found = [
        ScanResult(host="192.168.1.10", banner="SSH-2.0-OpenSSH_5.2", mac="bc:f3:10:7c:06:80", hostname="AH-7c0680"),
        ScanResult(host="192.168.1.20", banner="SSH-2.0-dropbear", mac="00:11:22:33:44:55"),
    ]
    result = await _scan_flow(hass, found)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"
    assert result["description_placeholders"] == {"count": "1", "total": "2"}
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.10", **CREDS}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.1.10"


async def test_scan_nothing_found_falls_back_to_manual(hass: HomeAssistant) -> None:
    result = await _scan_flow(hass, [])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["errors"] == {"base": "no_devices_found"}


# --------------------------------------------------------------------------
# SSID management on the AP
# --------------------------------------------------------------------------


async def test_ssid_hidden_and_all_radios_switches(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    hidden = _entity_id(hass, "switch", "ssid_hidden_AP460C")
    assert hass.states.get(hidden).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": hidden}, blocking=True)
    assert client.commands[:2] == ["ssid AP460C hide-ssid", "save config"]

    all_radios = _entity_id(hass, "switch", "ssid_all_AP460C")
    assert hass.states.get(all_radios).state == "on"
    client.commands.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": all_radios}, blocking=True)
    assert [c for c in client.commands if not c.startswith("show")] == [
        "no interface wifi0 ssid AP460C",
        "no interface wifi1 ssid AP460C",
        "save config",
    ]


async def test_wifi_key_text_enabled_by_default(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    state = hass.states.get(_entity_id(hass, "text", "psk_AP460C"))
    assert state is not None and state.state == "unknown"


async def test_create_and_delete_ssid(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    await hass.services.async_call(
        DOMAIN, "create_ssid",
        {"device_id": device.id, "ssid": "Guest", "password": "guest-pass-1", "radios": ["wifi1"]},
        blocking=True,
    )  # fmt: skip
    assert [c for c in client.commands if not c.startswith("show")] == [
        "security-object Guest",
        "security-object Guest security protocol-suite wpa2-aes-psk ascii-key guest-pass-1",
        "ssid Guest",
        "ssid Guest security-object Guest",
        "interface wifi1 ssid Guest",
        "save config",
    ]
    with pytest.raises(HomeAssistantError, match="already exists"):
        await hass.services.async_call(
            DOMAIN, "create_ssid", {"device_id": device.id, "ssid": "AP460C"}, blocking=True
        )
    with pytest.raises(HomeAssistantError, match="Unknown radio"):
        await hass.services.async_call(
            DOMAIN, "create_ssid", {"device_id": device.id, "ssid": "X", "radios": ["wifi9"]}, blocking=True
        )

    client.commands.clear()
    await hass.services.async_call(DOMAIN, "delete_ssid", {"device_id": device.id, "ssid": "AP460C"}, blocking=True)
    # security-object "sec-ap460c" is shared/named differently: kept.
    assert [c for c in client.commands if not c.startswith("show")] == [
        "no interface wifi0 ssid AP460C",
        "no interface wifi1 ssid AP460C",
        "no ssid AP460C",
        "save config",
    ]


async def test_enable_all_entities_option(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    raw = _entity_id(hass, "sensor", "raw_if_wifi0_mtu")
    assert registry.async_get(raw).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, "enable_all_entities": True}
    )
    await hass.async_block_till_done()
    assert registry.async_get(raw).disabled_by is None
    assert registry.async_get(_entity_id(hass, "sensor", "iface_eth1_rx_bytes")).disabled_by is None


# --------------------------------------------------------------------------
# SR2208P switch
# --------------------------------------------------------------------------


def _switch_responses() -> dict[str, str]:
    return {
        "show version": load_fixture("synthetic_sr2208p_show_version.txt"),
        "show interface": load_fixture("synthetic_sr2208p_show_interface.txt"),
        "show ssid": "            ^-- unknown keyword or invalid input",
        "show station": "            ^-- unknown keyword or invalid input",
        "show pse": load_fixture("synthetic_sr2208p_show_pse.txt"),
        "show mac-address-table": load_fixture("synthetic_sr2208p_show_mac_table.txt"),
        "show lldp neighbor": load_fixture("synthetic_sr2208p_show_lldp.txt"),
        "show interface eth1/1": "Speed=1000Mbps; Duplex=full-duplex;\nRx bytes=1000; Tx bytes=2000;",
        "show running-config": "interface eth1/4 pse shutdown\n",
        "show cpu": "CPU total utilization: 12.0%",
    }


@pytest.fixture
async def switch_entry(hass: HomeAssistant) -> MockConfigEntry:
    original = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.hostname = "AH-001000"
        self.responses = _switch_responses()

    with patch.object(FakeClient, "__init__", init):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=SWITCH_MAC,
            data={**USER_INPUT, CONF_HOST: "192.168.1.30"}, title="AH-001000",
            options={CONF_TRACKED_CLIENTS: ["a4:c3:f0:aa:00:02"]},
        )  # fmt: skip
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _sw(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SWITCH_MAC}_{key}")
    assert entity_id, f"no {platform} entity {key}"
    return entity_id


async def test_switch_entities(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SWITCH_MAC)})
    assert device.model == "SR2208P"
    st = hass.states.get
    assert st(_sw(hass, "binary_sensor", "iface_eth1/1")).state == "on"
    assert st(_sw(hass, "binary_sensor", "iface_eth1/3")).state == "off"
    assert st(_sw(hass, "sensor", "iface_eth1/1_link_speed")).state == "1000"
    assert st(_sw(hass, "sensor", "iface_eth1/2_link_speed")).state == "100"
    assert st(_sw(hass, "sensor", "iface_eth1/1_poe_power")).state == "6.5"
    assert st(_sw(hass, "sensor", "iface_eth1/1_poe_status")).state == "delivering"
    assert st(_sw(hass, "sensor", "poe_power_used")).state == "13.2"
    assert st(_sw(hass, "sensor", "poe_power_budget")).state == "125.0"
    assert st(_sw(hass, "sensor", "poe_power_remaining")).state == "111.8"
    assert st(_sw(hass, "sensor", "poe_ports_active")).state == "2"
    assert st(_sw(hass, "sensor", "wired_clients")).state == "4"
    assert st(_sw(hass, "sensor", "iface_eth1/1_wired_clients")).state == "2"
    assert st(_sw(hass, "sensor", "iface_eth1/1_lldp_neighbor")).state == "AH-7c0680"
    assert st(_sw(hass, "sensor", "cpu_usage")).state == "12.0"
    assert st(_sw(hass, "switch", "poe_eth1/1")).state == "on"
    assert st(_sw(hass, "switch", "poe_eth1/4")).state == "off"
    assert _sw(hass, "button", "poe_cycle_eth1/1")
    # Port enable switches exist but are disabled by default (lock-out risk).
    port_switch = er.async_get(hass).async_get(_sw(hass, "switch", "port_eth1/1"))
    assert port_switch.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # No Wi-Fi entities on a switch.
    ents = er.async_entries_for_config_entry(er.async_get(hass), switch_entry.entry_id)
    assert not [e for e in ents if "wifi" in e.unique_id or "ssid" in e.unique_id]
    # Wired client tracked via the MAC address table.
    tracker = st(_sw(hass, "device_tracker", "tracker_a4:c3:f0:aa:00:02"))
    assert tracker.state == "home"
    assert tracker.attributes["port"] == "eth1/2"


async def test_switch_poe_control(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    client = FakeClient.instances[-1]
    client.commands.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": _sw(hass, "switch", "poe_eth1/2")}, blocking=True)
    assert client.commands[:2] == ["interface eth1/2 pse shutdown", "save config"]

    client.commands.clear()
    with patch("custom_components.aerohive_local.coordinator.asyncio.sleep", return_value=None):
        await hass.services.async_call(
            "button", "press", {"entity_id": _sw(hass, "button", "poe_cycle_eth1/1")}, blocking=True
        )
    assert client.commands[:2] == ["interface eth1/1 pse shutdown", "no interface eth1/1 pse shutdown"]
    assert "save config" not in client.commands
