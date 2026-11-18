"""An OpenWrt router in Home Assistant: polling, entities, Wi-Fi changes, panel snapshot (synthetic fixtures)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from .conftest import HAS_HA, load_fixture

pytestmark = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")

if HAS_HA:
    from homeassistant.const import CONF_HOST
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local import openwrt as ow
    from custom_components.aerohive_local.const import DOMAIN
    from custom_components.aerohive_local.snapshot import device_kind

    from .test_init import USER_INPUT, FakeClient, fake_client  # noqa: F401

OW_MAC = "02:5a:00:00:00:01"


def _responses() -> dict[str, str]:
    r = {
        ow.BOARD: load_fixture("synthetic_openwrt_system_board.json"),
        ow.SYSTEM_INFO: load_fixture("synthetic_openwrt_system_info.json"),
        ow.NETWORK_DEVICES: load_fixture("synthetic_openwrt_network_device_status.json"),
        ow.NETWORK_INTERFACES: load_fixture("synthetic_openwrt_network_interface_dump.json"),
        ow.CPU_STAT: "cpu  100 0 100 800 0 0 0 0 0 0",
        ow.UCI_WIRELESS: load_fixture("synthetic_openwrt_uci_show_wireless.txt"),
        ow.WIRELESS_STATUS: load_fixture("synthetic_openwrt_network_wireless_status.json"),
        ow.DHCP_LEASES: load_fixture("synthetic_openwrt_dhcp_leases.txt"),
        ow.ARP: load_fixture("synthetic_openwrt_proc_net_arp.txt"),
        ow.UCI_CHANGES: "",
        ow.CONFIG_EXPORT: load_fixture("synthetic_openwrt_uci_export.txt"),
        ow.TEMPERATURE: "51200",
    }
    for dev in ("phy0-ap0", "phy1-ap0", "phy1-ap1"):
        r[ow.iwinfo_info(dev)] = load_fixture(f"synthetic_openwrt_iwinfo_info_{dev}.json")
        r[ow.iwinfo_assoclist(dev)] = load_fixture(f"synthetic_openwrt_iwinfo_assoclist_{dev}.json")
    return r


@pytest.fixture
async def ow_entry(hass: HomeAssistant):
    original_init = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.hostname = "OpenWrt"
        self.dialect = "openwrt"
        self.responses = _responses()

    with patch.object(FakeClient, "__init__", init):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=OW_MAC, title="OpenWrt", minor_version=3, data={**USER_INPUT, CONF_HOST: "192.168.1.1"}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


def _client() -> FakeClient:
    return FakeClient.instances[-1]


def _changes() -> list[str]:
    return [c for c in _client().commands if c.startswith(("uci set", "uci delete", "s=$(", "ubus call hostapd"))]


async def test_openwrt_router_is_read(hass: HomeAssistant, ow_entry: MockConfigEntry) -> None:
    data = ow_entry.runtime_data.data
    assert data.cli == "openwrt" and device_kind(data) == "router"
    assert data.hostname == "OpenWrt" and data.version.version == "OpenWrt 23.05.3 r23809-234f1a2efa"
    assert data.version.platform == "Example AX3000 Router" and data.mac == OW_MAC
    # Ports: the DSA switch ports, the WAN one marked as the uplink.
    assert data.ports() == ["wan", "lan1", "lan2", "lan3", "lan4"]
    assert data.interfaces["lan1"].is_up and data.interfaces["lan1"].speed == "1000"
    assert data.interfaces["lan2"].is_up is False and "wan" in data.uplink_ports()
    assert data.mgt.ip_address == "192.168.1.1" and data.mgt.netmask == "255.255.255.0"
    assert data.memory.memory_used_percent == pytest.approx(33.3, abs=0.1)
    # Wi-Fi: two radios, actual channels from iwinfo, four networks (IoT is off).
    assert data.radio_names() == ["radio0", "radio1"]
    assert data.interfaces["radio0"].channel == 6 and data.details["radio1"].tx_power == 23
    assert sorted(data.ssids) == ["Guest Net", "Home Net", "IoT"]
    assert data.ssid_on_radio("Home Net", "radio0") and data.ssid_on_radio("Home Net", "radio1")
    assert data.ssid_on_radio("Guest Net", "radio1") and not data.ssid_on_radio("Guest Net", "radio0")
    assert not data.ssid_on_radio("IoT", "radio0") and data.ssid_hidden("Guest Net")
    assert data.ssids["Guest Net"].max_client == 20 and data.config.ssid_security("Home Net") == "sae-mixed"
    # Clients, named from the DHCP leases.
    assert sorted(data.stations) == ["02:11:22:33:44:01", "02:11:22:33:44:02", "02:11:22:33:44:03"]
    phone = data.stations["02:11:22:33:44:01"]
    assert (phone.hostname, phone.ip, phone.ssid, phone.radio, phone.signal) == ("Pixel-8", "192.168.1.101", "Home Net", "radio0", -52)
    assert phone.tx_rate == "172M" and phone.assoc_time == "1:02:05"
    assert data.clients_on_ssid("Home Net") == 3
    # The NAS is a wired client (in ARP, not on Wi-Fi); the ISP's router on the WAN isn't.
    assert data.wired_client_macs() == {"02:aa:bb:cc:dd:01"}
    assert data.environment.temperature_c == 51.2
    assert data.config_unsaved is False
    # A backup was taken, with the Wi-Fi key masked.
    backups = ow_entry.runtime_data.backup_folder
    files = list(backups.glob("*.cfg"))
    assert len(files) == 1 and "option key '***'" in files[0].read_text()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, OW_MAC)})
    assert device.model == "Example AX3000 Router" and device.sw_version.startswith("OpenWrt 23.05.3")


async def test_openwrt_entities(hass: HomeAssistant, ow_entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, ow_entry.entry_id)}
    for key in ("iface_lan1", "ssid_radio1_Guest Net", "ssid_hidden_Guest Net", "reboot", "refresh"):
        assert f"{OW_MAC}_{key}" in ids, key
    # Ports are shown, not switched (OpenWrt port control isn't offered).
    assert not [i for i in ids if i.startswith(f"{OW_MAC}_port_")]


async def test_openwrt_wifi_changes(hass: HomeAssistant, ow_entry: MockConfigEntry) -> None:
    coordinator = ow_entry.runtime_data
    _client().commands.clear()
    # Turn IoT on (the disabled section is enabled), on 2.4 GHz only.
    assert await coordinator.async_update_ssid("IoT", radios=["radio0"])
    assert _changes()[-1] == f"uci set wireless.cfg083579.disabled=0 && {ow.WIFI_APPLY} || echo '{ow.FAILED_MARK}'"
    # Put Guest Net on 2.4 GHz too: a new section copies the 5 GHz one (key copied on the router).
    _client().commands.clear()
    await coordinator.async_set_ssid_on_radio("radio0", "Guest Net", True)
    assert _changes()[-1].startswith("s=$(uci add wireless wifi-iface) && uci set wireless.$s.device=radio0")
    assert "uci -q get wireless.cfg073579.$o" in _changes()[-1]
    # Turn Home Net off on 5 GHz, hide it, new password: one line, one reload.
    _client().commands.clear()
    await coordinator.async_update_ssid("Home Net", radios=["radio0"], hidden=True, password="correct horse 9")
    line = _changes()[-1]
    assert "uci set wireless.default_radio1.disabled=1" in line and "default_radio0.disabled" not in line
    assert "uci set wireless.default_radio0.hidden=1" in line and "uci set wireless.default_radio1.key='correct horse 9'" in line
    assert line.count("wifi reload") == 1
    # Create and delete.
    _client().commands.clear()
    await coordinator.async_create_ssid("Kids", "bedtime-2026", ["radio1"], False)
    assert _changes()[-1].startswith("s=$(uci add wireless wifi-iface) && uci set wireless.$s.device=radio1")
    assert "uci set wireless.$s.network=lan" in _changes()[-1]
    _client().commands.clear()
    await coordinator.async_delete_ssid("Guest Net")
    assert _changes()[-1].startswith("uci delete wireless.cfg073579 && uci commit wireless")
    # Rejected changes are reported.
    _client().responses[f"uci set wireless.cfg083579.hidden=1 && {ow.WIFI_APPLY} || echo '{ow.FAILED_MARK}'"] = ow.FAILED_MARK
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await coordinator.async_set_ssid_hidden("IoT", True)
    with pytest.raises(HomeAssistantError, match="8"):
        await coordinator.async_set_ssid_password("Home Net", "short")


async def test_openwrt_disconnect_reboot_and_limits(hass: HomeAssistant, ow_entry: MockConfigEntry) -> None:
    coordinator = ow_entry.runtime_data
    _client().commands.clear()
    await coordinator.async_disconnect_client("02:11:22:33:44:02")
    assert _changes() == [
        "ubus call hostapd.phy1-ap0 del_client "
        "'{\"addr\":\"02:11:22:33:44:02\",\"reason\":5,\"deauth\":true,\"ban_time\":0}' "
        f"|| echo '{ow.FAILED_MARK}'"
    ]
    await coordinator.async_reboot()
    assert _client().commands[-1] == "reboot"
    with pytest.raises(HomeAssistantError, match="OpenWrt"):
        await coordinator.async_set_port_enabled("lan1", False)
    with pytest.raises(HomeAssistantError, match="LuCI"):
        await coordinator.async_restore_config()


async def test_openwrt_unsaved_changes_and_snapshot(hass: HomeAssistant, hass_ws_client, ow_entry: MockConfigEntry) -> None:
    coordinator = ow_entry.runtime_data
    _client().responses[ow.UCI_CHANGES] = "wireless.cfg083579.disabled='0'"
    await coordinator.async_refresh()
    assert coordinator.data.config_unsaved is True
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "aerohive_local/snapshot"})
    snap = (await client.receive_json())["result"]
    device = snap["devices"][0]
    assert device["kind"] == "router" and device["features"]["port_control"] is False
    assert [p["name"] for p in device["ports"]] == ["wan", "lan1", "lan2", "lan3", "lan4"]
    assert next(p for p in device["ports"] if p["name"] == "wan")["uplink"]
    assert {s["name"] for s in device["ssids"]} == {"Home Net", "Guest Net", "IoT"}
    assert device["config"]["unsaved"] is True
    # Nothing secret reaches the panel.
    assert "key" not in json.dumps(device["ssids"]) and "correct horse" not in json.dumps(snap)


async def test_config_flow_detects_openwrt(hass: HomeAssistant) -> None:
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    original_init = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.hostname = "OpenWrt"
        self.dialect = "openwrt"
        self.responses = _responses()

    with patch.object(FakeClient, "__init__", init), patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.1", "username": "root", "password": "x", "port": 22}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenWrt" and result["result"].unique_id == OW_MAC


async def test_dialect_known_before_first_poll(hass: HomeAssistant) -> None:
    """A new session learns it's OpenWrt from the login prompt; the first poll must already use OpenWrt commands."""
    original_init = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.connected = False
        self.dialect = "hiveos"  # the default until the prompt is seen
        self.dialect_on_connect = "openwrt"
        self.hostname = "OpenWrt"
        self.responses = _responses()

    with patch.object(FakeClient, "__init__", init):
        entry = MockConfigEntry(domain=DOMAIN, unique_id=OW_MAC, title="OpenWrt", minor_version=3, data=USER_INPUT)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success
    assert entry.runtime_data.data.cli == "openwrt"
    assert "show version" not in FakeClient.instances[-1].commands


async def test_turning_a_network_back_on_keeps_its_bands(hass: HomeAssistant, ow_entry: MockConfigEntry) -> None:
    """IoT is configured for 2.4 GHz only (and off); switching it on must not add it to 5 GHz."""
    from custom_components.aerohive_local import wifi

    coordinator = ow_entry.runtime_data
    assert coordinator.wifi_home_radios("IoT") == ["radio0"]
    _client().commands.clear()
    await wifi.async_update_network(hass, "IoT", enabled=True)
    assert _changes() == [f"uci set wireless.cfg083579.disabled=0 && {ow.WIFI_APPLY} || echo '{ow.FAILED_MARK}'"]
    # Picking bands still adds it where asked.
    _client().commands.clear()
    await wifi.async_update_network(hass, "IoT", enabled=True, bands=["2.4 GHz", "5 GHz"])
    assert "uci set wireless.$s.device=radio1" in _changes()[-1]


async def test_management_address_is_never_loopback(hass: HomeAssistant) -> None:
    original_init = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.host = "127.0.0.1"  # e.g. reached through an SSH tunnel
        self.hostname = "OpenWrt"
        self.dialect = "openwrt"
        self.responses = _responses()

    with patch.object(FakeClient, "__init__", init):
        entry = MockConfigEntry(domain=DOMAIN, unique_id=OW_MAC, title="OpenWrt", minor_version=3, data=USER_INPUT)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.data.mgt.ip_address == "192.168.1.1"
