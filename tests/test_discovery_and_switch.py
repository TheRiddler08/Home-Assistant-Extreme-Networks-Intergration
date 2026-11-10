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

from .test_init import MAC, USER_INPUT, FakeClient, _client, _entity_id, _open_settings, entry, fake_client  # noqa: E402, F401

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
    result = await _open_settings(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, "enable_all_entities": True}
    )
    await hass.async_block_till_done()
    assert registry.async_get(raw).disabled_by is None
    assert registry.async_get(_entity_id(hass, "sensor", "iface_eth1_rx_bytes")).disabled_by is None


# --------------------------------------------------------------------------
# SR2208P switch (real output from an SR2208P, software 1.0.1.28, anonymised)
# --------------------------------------------------------------------------

SWITCH_MAC = "b8:7c:f2:00:00:94"


def _switch_responses() -> dict[str, str]:
    detail = "show lldp remote-device detail 1/0/2"
    return {
        "show version": load_fixture("sr2208p_show_version.txt"),
        "show port all": load_fixture("sr2208p_show_port_all.txt"),
        "show network": load_fixture("sr2208p_show_network.txt"),
        "show poe": load_fixture("sr2208p_show_poe.txt"),
        "show mac-addr-table": load_fixture("sr2208p_show_mac_addr_table.txt"),
        "show lldp remote-device all": load_fixture("sr2208p_show_lldp_remote_device_all.txt"),
        detail: load_fixture("sr2208p_show_lldp_remote_device_detail_1_0_2.txt"),
        # Per-port PoE: real 'show poe port info 1/0/N' output (1/0/1), adapted for 1/0/2-1/0/8.
        **{f"show poe port info 1/0/{i}": _poe_info(i) for i in range(1, 9)},
        "show interface ethernet 1/0/1": load_fixture("sr2208p_show_interface_ethernet_1_0_1.txt"),
        "show running-config": load_fixture("sr2208p_show_running_config.txt")
        + "interface 1/0/3\nno poe\nexit\ninterface 1/0/5\nshutdown\nexit\n",
    }


def _poe_info(port: int) -> str:
    text = load_fixture("sr2208p_show_poe_port_info_1_0_1.txt").replace("1/0/1  ", f"1/0/{port}  ")
    if port == 2:  # an AP drawing 8.8 W
        text = text.replace("Unknown  0         0       0 ", "Class4   8800      183     48").replace(
            "Searching ", "Delivering"
        )
    return text


UNSUPPORTED = "                   ^\n% Invalid input detected at '^' marker."


@pytest.fixture
async def switch_entry(hass: HomeAssistant) -> MockConfigEntry:
    original = FakeClient.__init__
    original_run = FakeClient.run

    def init(self, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.hostname = "AH-Switch"
        self.dialect = "switch"
        self.responses = _switch_responses()

    async def run(self, command: str, **kwargs: Any) -> str:
        out = await original_run(self, command, **kwargs)
        if command.startswith("show") and command not in self.responses:
            return UNSUPPORTED
        return out

    with patch.object(FakeClient, "__init__", init), patch.object(FakeClient, "run", run):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=SWITCH_MAC,
            data={**USER_INPUT, CONF_HOST: "192.168.9.64"}, title="AH-Switch",
            options={CONF_TRACKED_CLIENTS: ["b8:a4:4f:00:d3:de", "00:15:5d:00:00:01"]},
        )  # fmt: skip
        entry.add_to_hass(hass)
        with patch("custom_components.aerohive_local.coordinator.discovery_flow.async_create_flow") as flows:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        entry.discovery_calls = flows.call_args_list  # type: ignore[attr-defined]
        yield entry


def _sw(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SWITCH_MAC}_{key}")
    assert entity_id, f"no {platform} entity {key}"
    return entity_id


async def test_switch_entities(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SWITCH_MAC)})
    assert device.model == "SR2208P"
    assert device.sw_version == "1.0.1.28"
    assert device.serial_number == "22081600000000"
    assert device.configuration_url == "https://192.168.9.64"
    st = hass.states.get
    assert st(_sw(hass, "binary_sensor", "iface_1/0/1")).state == "on"
    assert st(_sw(hass, "binary_sensor", "iface_1/0/3")).state == "off"
    assert er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{SWITCH_MAC}_iface_0/3/1") is None
    assert st(_sw(hass, "sensor", "iface_1/0/1_link_speed")).state == "1000"
    assert st(_sw(hass, "sensor", "iface_1/0/4_link_speed")).state == "100"
    assert st(_sw(hass, "sensor", "iface_1/0/4_duplex")).state == "Full"
    assert st(_sw(hass, "sensor", "ip_address")).state == "192.168.9.64"
    assert st(_sw(hass, "sensor", "poe_power_used")).state == "15.3"
    assert st(_sw(hass, "sensor", "poe_power_budget")).state == "130.0"
    assert st(_sw(hass, "sensor", "poe_power_remaining")).state == "114.7"
    assert st(_sw(hass, "sensor", "iface_1/0/2_poe_power")).state == "8.8"
    assert st(_sw(hass, "sensor", "iface_1/0/2_poe_status")).state == "Delivering"
    assert st(_sw(hass, "sensor", "iface_1/0/1_poe_status")).state == "Searching"
    assert st(_sw(hass, "sensor", "iface_1/0/1_poe_fault")).state == "No Error"
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SWITCH_MAC}_iface_1/0/9_poe_power") is None
    assert st(_sw(hass, "sensor", "poe_ports_active")).state == "1"
    # Real 'show interface ethernet' counters (byte counters -> throughput after 2 polls).
    rx_bytes = er.async_get(hass).async_get(_sw(hass, "sensor", "iface_1/0/1_rx_bytes"))
    assert rx_bytes.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    coordinator = switch_entry.runtime_data
    assert coordinator.data.details["1/0/1"].rx_bytes == 297039
    assert coordinator.data.details["1/0/1"].tx_bytes == 9706311
    assert coordinator.data.details["1/0/1"].tx_packets == 109582
    assert coordinator.data.details["1/0/1"].mtu == 1500
    assert st(_sw(hass, "sensor", "iface_1/0/1_rx_throughput")).state == "unknown"
    # Uptime from the running-config header.
    assert coordinator.data.version.uptime_seconds >= 5739
    assert st(_sw(hass, "sensor", "boot_time")).state not in ("unknown", "unavailable")
    # No raw sensors for the ~70 per-port switch counters.
    raw = [e for e in er.async_entries_for_config_entry(er.async_get(hass), switch_entry.entry_id)
           if e.unique_id.startswith(f"{SWITCH_MAC}_raw_if_1/0/")]
    assert raw == []
    assert st(_sw(hass, "switch", "poe_1/0/1")).state == "on"
    assert st(_sw(hass, "switch", "poe_1/0/3")).state == "off"
    # LLDP: summary + detail (management address of the neighbouring AP).
    lldp = st(_sw(hass, "sensor", "iface_1/0/2_lldp_neighbor"))
    assert lldp.state == "AH-112200"
    assert lldp.attributes["management_address"] == "192.168.9.73"
    assert st(_sw(hass, "sensor", "iface_1/0/1_lldp_neighbor")).state == "PC-9"
    # MAC table: uplink 1/0/9 (21 MACs, LLDP neighbour core-switch) excluded from wired clients.
    assert st(_sw(hass, "sensor", "iface_1/0/9_wired_clients")).state == "21"
    assert st(_sw(hass, "sensor", "iface_1/0/7_wired_clients")).state == "3"
    assert st(_sw(hass, "sensor", "wired_clients")).state == "9"
    # Port switches are enabled; 1/0/5 is shut down in the running config.
    assert st(_sw(hass, "switch", "port_1/0/5")).state == "off"
    assert st(_sw(hass, "switch", "port_1/0/1")).state == "on"
    assert st(_sw(hass, "switch", "port_1/0/9")).attributes["uplink"] is True
    # No Wi-Fi entities on a switch.
    ents = er.async_entries_for_config_entry(er.async_get(hass), switch_entry.entry_id)
    assert not [e for e in ents if "wifi" in e.unique_id or "ssid" in e.unique_id]
    # Trackers: camera on an access port is home; a MAC behind the uplink is not.
    cam = st(_sw(hass, "device_tracker", "tracker_b8:a4:4f:00:d3:de"))
    assert cam.state == "home" and cam.attributes["port"] == "1/0/4"
    assert st(_sw(hass, "device_tracker", "tracker_00:15:5d:00:00:01")).state == "not_home"


async def test_switch_lldp_offers_unconfigured_ap(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    calls = switch_entry.discovery_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0].kwargs["data"] == {CONF_HOST: "192.168.9.73", "mac": "80:95:62:11:22:00", "name": "AH-112200"}


async def test_switch_poe_and_port_control(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    client = FakeClient.instances[-1]
    client.commands.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": _sw(hass, "switch", "poe_1/0/2")}, blocking=True)
    assert client.commands[:6] == ["configure", "interface 1/0/2", "no poe", "exit", "exit", "write memory"]

    client.commands.clear()
    with patch("custom_components.aerohive_local.coordinator.asyncio.sleep", return_value=None):
        await hass.services.async_call(
            "button", "press", {"entity_id": _sw(hass, "button", "poe_cycle_1/0/1")}, blocking=True
        )
    assert client.commands[:5] == ["configure", "interface 1/0/1", "poe reset", "exit", "exit"]
    assert "write memory" not in client.commands

    # Firmware without "poe reset": fall back to off, wait, on.
    client.responses["poe reset"] = "% Invalid input detected at '^' marker."
    client.commands.clear()
    with patch("custom_components.aerohive_local.coordinator.asyncio.sleep", return_value=None):
        await hass.services.async_call(
            "button", "press", {"entity_id": _sw(hass, "button", "poe_cycle_1/0/1")}, blocking=True
        )
    # (the real client leaves config mode after the rejected block; see test_client)
    assert client.commands[:11] == [
        "configure", "interface 1/0/1", "poe reset",
        "configure", "interface 1/0/1", "no poe", "exit", "exit",
        "configure", "interface 1/0/1", "poe",
    ]  # fmt: skip
    client.responses.pop("poe reset")

    # Uplink protection: 1/0/9 leads to the core switch.
    client.commands.clear()
    with pytest.raises(HomeAssistantError, match="uplink"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": _sw(hass, "switch", "port_1/0/9")}, blocking=True
        )
    assert client.commands == [] or all(c.startswith("show") for c in client.commands)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SWITCH_MAC)})
    await hass.services.async_call(
        DOMAIN, "set_port", {"device_id": device.id, "port": "1/0/9", "enabled": False, "force": True}, blocking=True
    )
    assert client.commands[:6] == ["configure", "interface 1/0/9", "shutdown", "exit", "exit", "write memory"]

    client.commands.clear()
    client.responses["interface 1/0/4"] = "An invalid interface has been used for this command."
    client.commands.clear()
    with pytest.raises(HomeAssistantError, match="invalid interface"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": _sw(hass, "switch", "port_1/0/4")}, blocking=True
        )
    assert "write memory" not in client.commands


async def test_integration_discovery_from_lldp(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_HOST: "192.168.9.73", "mac": "80:95:62:11:22:00", "name": "AH-112200"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"]["name"] == "AH-112200"


async def test_config_flow_switch_uses_burned_in_mac(hass: HomeAssistant) -> None:
    original = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.hostname = "AH-Switch"
        self.dialect = "switch"
        self.responses = _switch_responses()

    with patch.object(FakeClient, "__init__", init):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
        with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.9.64"}
            )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AH-Switch"
    assert result["result"].unique_id == SWITCH_MAC
    assert "show interface" not in FakeClient.instances[-1].commands


# --------------------------------------------------------------------------
# On-screen SSID management (per AP) and the network overview
# --------------------------------------------------------------------------


def _id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id, f"no {platform} entity {unique_id}"
    return entity_id


async def test_overview_combines_ap_and_switch(
    hass: HomeAssistant, entry: MockConfigEntry, switch_entry: MockConfigEntry
) -> None:
    await hass.async_block_till_done()
    overview = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("entry_type") == "overview"]
    assert len(overview) == 1
    await overview[0].runtime_data.async_refresh()
    await hass.async_block_till_done()
    st = hass.states.get
    assert st(_id(hass, "sensor", "overview_devices_online")).state == "2"
    assert st(_id(hass, "sensor", "overview_aps_online")).state == "1"
    assert st(_id(hass, "sensor", "overview_switches_online")).state == "1"
    clients = st(_id(hass, "sensor", "overview_wireless_clients"))
    assert clients.state == "3"
    assert {c["ap"] for c in clients.attributes["clients"]} == {"AH-7c0680"}
    assert st(_id(hass, "sensor", "overview_clients_5ghz")).state == "1"
    assert st(_id(hass, "sensor", "overview_wired_clients")).state == "9"
    assert st(_id(hass, "sensor", "overview_poe_power_used")).state == "15.3"
    ssids = st(_id(hass, "sensor", "overview_ssids"))
    assert ssids.state == "1"
    assert ssids.attributes["ssids"][0]["ssid"] == "AP460C"
    assert ssids.attributes["ssids"][0]["access_points"] == ["AH-7c0680"]
    assert st(_id(hass, "sensor", "overview_ssid_clients_AP460C")).state == "3"
    assert st(_id(hass, "binary_sensor", "overview_all_online")).state == "on"
    assert st(_id(hass, "switch", "overview_ssid_all_AP460C")).state == "on"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "overview")})
    assert device.name == "Aerohive Network"

    # Change the Wi-Fi key on every AP at once.
    ap_client = next(c for c in FakeClient.instances if c.dialect == "hiveos")
    ap_client.commands.clear()
    await hass.services.async_call(
        "text", "set_value",
        {"entity_id": _id(hass, "text", "overview_psk_AP460C"), "value": "network-wide-key"}, blocking=True,
    )  # fmt: skip
    assert "security-object sec-ap460c security protocol-suite wpa2-aes-psk ascii-key network-wide-key" in ap_client.commands

    # Turn the SSID off everywhere.
    ap_client.commands.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _id(hass, "switch", "overview_ssid_all_AP460C")}, blocking=True
    )
    assert "no interface wifi0 ssid AP460C" in ap_client.commands
    assert "no interface wifi1 ssid AP460C" in ap_client.commands

    # The switch never received any Wi-Fi command.
    sw_client = next(c for c in FakeClient.instances if c.dialect == "switch")
    assert not [c for c in sw_client.commands if "ssid" in c or "security-object" in c]


async def test_switch_port_summary(hass: HomeAssistant, switch_entry: MockConfigEntry) -> None:
    st = hass.states.get
    up = st(_sw(hass, "sensor", "ports_up"))
    assert up.state == "9"
    assert up.attributes["total"] == 10
    rows = {r["port"]: r for r in up.attributes["ports"]}
    assert rows["1/0/2"]["neighbor"] == "AH-112200" and rows["1/0/2"]["poe_w"] == 8.8
    assert rows["1/0/9"]["uplink"] is True
    assert st(_sw(hass, "sensor", "ports_down")).state == "1"
    assert st(_sw(hass, "sensor", "ports_disabled")).state == "1"  # 1/0/5 shut down in config
    assert st(_sw(hass, "sensor", "ports_errors")).state == "0"


# --------------------------------------------------------------------------
# Removing entities of SSIDs / clients that no longer exist
# --------------------------------------------------------------------------

NO_SSID_TABLE = (
    "No. Name                             Frag    RTS     DTIM period Max client  Mac filter\n"
    "--- ----                             ----    ---     ----------- ---------- ----------\n"
)


def _without_ap460c(client: FakeClient) -> None:
    """Make the fake AP report that SSID AP460C is gone."""
    client.responses["show ssid"] = NO_SSID_TABLE
    client.responses["show interface"] = "\n".join(
        line for line in load_fixture("ap460c_show_interface.txt").splitlines() if "AP460C" not in line
    )
    client.responses["show station"] = ""


def _ssid_entities(hass: HomeAssistant, entry_id: str, ssid: str = "AP460C") -> list[str]:
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        if e.unique_id.endswith(f"_{ssid}") or f"_{ssid}_" in e.unique_id
    ]


async def test_deleted_ssid_entities_are_removed_immediately(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    before = _ssid_entities(hass, entry.entry_id)
    assert len(before) >= 8  # switches, text, number, sensors, binary sensor...
    _without_ap460c(_client())
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    await hass.services.async_call(DOMAIN, "delete_ssid", {"device_id": device.id, "ssid": "AP460C"}, blocking=True)
    await hass.async_block_till_done()
    assert _ssid_entities(hass, entry.entry_id) == []
    for entity_id in before:
        assert hass.states.get(entity_id) is None  # gone, not "unavailable"
    # Re-creating the SSID brings the entities back.
    _client().responses.update({
        "show ssid": load_fixture("ap460c_show_ssid.txt"),
        "show interface": load_fixture("ap460c_show_interface.txt"),
    })  # fmt: skip
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(_ssid_entities(hass, entry.entry_id)) == len(before)


async def test_ssid_removed_on_device_after_two_polls(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data
    _without_ap460c(_client())
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _ssid_entities(hass, entry.entry_id)  # one odd poll: kept
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _ssid_entities(hass, entry.entry_id) == []


async def test_single_bad_reply_does_not_remove(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data
    client = _client()
    good = dict(client.responses)
    _without_ap460c(client)
    await coordinator.async_refresh()
    client.responses.update(good)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    count = len(_ssid_entities(hass, entry.entry_id))
    _without_ap460c(client)
    await coordinator.async_refresh()  # miss counter was reset by the good poll
    await hass.async_block_till_done()
    assert len(_ssid_entities(hass, entry.entry_id)) == count


async def test_leftovers_from_previous_runs_are_removed(hass: HomeAssistant) -> None:
    from .test_init import USER_INPUT as UI

    old = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=UI, title="AH-7c0680", minor_version=2)
    old.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create("text", DOMAIN, f"{MAC}_psk_OldGuest", config_entry=old)
    stale_switch = registry.async_get_or_create("switch", DOMAIN, f"{MAC}_ssid_wifi0_OldGuest", config_entry=old)
    unrelated = registry.async_get_or_create("sensor", DOMAIN, f"{MAC}_iface_eth9_rx_throughput", config_entry=old)
    assert await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get(stale_switch.entity_id) is None
    assert registry.async_get(unrelated.entity_id) is not None  # not an SSID/client entity
    assert registry.async_get_entity_id("text", DOMAIN, f"{MAC}_psk_AP460C") is not None


async def test_untracked_client_entities_are_removed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _open_settings(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_TRACKED_CLIENTS: ["e8:b1:fc:5a:1b:2c"]}
    )
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("device_tracker", DOMAIN, f"{MAC}_tracker_e8:b1:fc:5a:1b:2c")
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{MAC}_client_e8:b1:fc:5a:1b:2c_signal")
    result = await _open_settings(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_TRACKED_CLIENTS: []}
    )
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("device_tracker", DOMAIN, f"{MAC}_tracker_e8:b1:fc:5a:1b:2c") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{MAC}_client_e8:b1:fc:5a:1b:2c_signal") is None


async def test_overview_keeps_ssid_entities_while_ap_offline(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    from custom_components.aerohive_local.client import AerohiveConnectionError

    await hass.async_block_till_done()
    overview = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("entry_type") == "overview")
    switch_id = _id(hass, "switch", "overview_ssid_all_AP460C")
    _client().fail_with = AerohiveConnectionError("gone")
    await entry.runtime_data.async_refresh()
    for _ in range(3):
        await overview.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "unavailable"  # kept while the AP is offline
    _client().fail_with = None
    await entry.runtime_data.async_refresh()
    await overview.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "on"


# --------------------------------------------------------------------------
# Wi-Fi manager (Configure dialog)
# --------------------------------------------------------------------------


async def _open_wifi(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry_id)
    if result["type"] is FlowResultType.MENU:
        assert result["menu_options"] == ["wifi", "settings"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "wifi"
    return result


def _network_options(result: dict[str, Any]) -> dict[str, str]:
    selector = result["data_schema"].schema["network"]
    return {o["value"]: o["label"] for o in selector.config["options"]}


async def test_wifi_manager_lists_and_edits(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _open_wifi(hass, entry.entry_id)
    labels = _network_options(result)
    assert labels["AP460C"] == "AP460C — on wifi0 (2.4 GHz), wifi1 (5 GHz) · 3 clients"
    assert "__add__" in labels and "__done__" in labels

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "AP460C"})
    assert result["step_id"] == "wifi_edit"
    assert result["description_placeholders"]["ssid"] == "AP460C"

    client = _client()
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"password": "brand-new-key", "radios": ["wifi1"], "hidden": True, "max_clients": 30, "delete": False},
    )
    assert result["step_id"] == "wifi"
    assert "Saved changes to AP460C" in result["description_placeholders"]["message"]
    config_cmds = [c for c in client.commands if not c.startswith("show")]
    assert config_cmds == [
        "no interface wifi0 ssid AP460C",
        "ssid AP460C hide-ssid",
        "ssid AP460C max-client 30",
        "security-object sec-ap460c security protocol-suite wpa2-aes-psk ascii-key brand-new-key",
        "save config",  # one save for all changes
    ]

    # Nothing changed -> nothing sent.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "AP460C"})
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"radios": ["wifi0", "wifi1"], "hidden": False, "max_clients": 50, "delete": False}
    )
    assert "No changes" in result["description_placeholders"]["message"]
    assert [c for c in client.commands if not c.startswith("show")] == []

    # Bad password is rejected in the form.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "AP460C"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"password": "short", "radios": ["wifi0", "wifi1"], "delete": False}
    )
    assert result["step_id"] == "wifi_edit" and result["errors"] == {"password": "invalid_password"}

    # Device error is shown in the form.
    client.responses["no interface wifi1 ssid AP460C"] = "ERROR: radio busy"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"radios": ["wifi0"], "delete": False}
    )
    assert result["errors"] == {"base": "device_error"}
    assert "radio busy" in result["description_placeholders"]["error"]


async def test_wifi_manager_add_and_delete(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    result = await _open_wifi(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "__add__"})
    assert result["step_id"] == "wifi_add"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": "AP460C", "radios": []})
    assert result["errors"] == {"name": "ssid_exists"}
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Guest", "password": "guest-pass-1", "radios": ["wifi1"], "hidden": True}
    )
    assert result["step_id"] == "wifi"
    assert [c for c in client.commands if not c.startswith("show")] == [
        "security-object Guest",
        "security-object Guest security protocol-suite wpa2-aes-psk ascii-key guest-pass-1",
        "ssid Guest",
        "ssid Guest security-object Guest",
        "interface wifi1 ssid Guest",
        "ssid Guest hide-ssid",
        "save config",
    ]

    # Delete needs confirmation.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "AP460C"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"delete": True})
    assert result["step_id"] == "wifi_delete"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": False})
    assert result["step_id"] == "wifi_edit"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"delete": True})
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": True})
    assert result["step_id"] == "wifi"
    assert "Deleted AP460C" in result["description_placeholders"]["message"]
    assert "no ssid AP460C" in client.commands

    # Done closes the dialog without changing options (no reload).
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "__done__"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_wifi_manager_overview(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.async_block_till_done()
    overview = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("entry_type") == "overview")
    await overview.runtime_data.async_refresh()
    result = await _open_wifi(hass, overview.entry_id)  # no menu: straight to the list
    assert _network_options(result)["AP460C"] == "AP460C — broadcasting on 1 of 1 APs · 3 clients"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "AP460C"})
    client = _client()
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"password": "everywhere-key", "enabled": False, "hidden": False, "delete": False}
    )
    assert result["step_id"] == "wifi"
    assert [c for c in client.commands if not c.startswith("show")] == [
        "no interface wifi0 ssid AP460C",
        "no interface wifi1 ssid AP460C",
        "security-object sec-ap460c security protocol-suite wpa2-aes-psk ascii-key everywhere-key",
        "save config",
    ]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"network": "__add__"})
    assert result["step_id"] == "wifi_add"
    client.commands.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Staff", "access_points": ["AH-7c0680"], "hidden": False}
    )
    assert "Created Staff (open network" in result["description_placeholders"]["message"]
    assert "ssid Staff" in client.commands


async def test_migration_removes_old_ssid_manager_entities(hass: HomeAssistant) -> None:
    old = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=USER_INPUT, title="AH-7c0680", minor_version=2)
    old.add_to_hass(hass)
    registry = er.async_get(hass)
    leftovers = [
        registry.async_get_or_create("text", DOMAIN, f"{MAC}_new_ssid_name", config_entry=old),
        registry.async_get_or_create("button", DOMAIN, f"{MAC}_create_ssid", config_entry=old),
        registry.async_get_or_create("select", DOMAIN, f"{MAC}_ssid_delete_target", config_entry=old),
    ]
    assert await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()
    assert old.minor_version == 3
    assert all(registry.async_get(e.entity_id) is None for e in leftovers)
