"""Home Assistant integration tests (run only when HA test deps are installed)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest

from .conftest import HAS_HA, load_fixture

if not HAS_HA:
    pytest.skip("Home Assistant test dependencies not installed", allow_module_level=True)

from homeassistant import config_entries  # noqa: E402
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.aerohive_local.client import AerohiveAuthError, AerohiveConnectionError  # noqa: E402
from custom_components.aerohive_local.const import (  # noqa: E402
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_CLIENTS,
    DOMAIN,
)
from custom_components.aerohive_local.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)

MAC = "bc:f3:10:7c:06:80"
USER_INPUT = {
    CONF_HOST: "192.168.1.10",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "hunter2-device",
    CONF_PORT: 22,
    CONF_SCAN_INTERVAL: 60,
}


def _responses() -> dict[str, str]:
    return {
        "show version": load_fixture("ap460c_show_version.txt"),
        "show interface": load_fixture("ap460c_show_interface.txt"),
        "show ssid": load_fixture("ap460c_show_ssid.txt"),
        "show station": load_fixture("synthetic_show_station.txt"),
        "show interface wifi0": load_fixture("synthetic_show_interface_wifi0.txt"),
        "show interface wifi1": "Mode=access; Radio profile=radio_axa0;\nOperational channel=36;",
        "show boot-param": load_fixture("synthetic_show_boot_param.txt"),
        "show running-config": "ssid AP460C security-object sec-ap460c\n",
        "show interface eth1": load_fixture("synthetic_show_interface_eth1.txt"),
        "show interface wifi0.1": load_fixture("synthetic_show_interface_wifi0_1.txt"),
        "show interface wifi1.1": "Rx bytes=100; Tx bytes=200;",
        "show interface mgt0": load_fixture("synthetic_show_interface_mgt0.txt"),
        "show cpu": load_fixture("synthetic_show_cpu.txt"),
        "show memory": load_fixture("synthetic_show_memory.txt"),
        "show hw-info": load_fixture("synthetic_show_hw_info.txt"),
    }


class FakeClient:
    """Stand-in for AerohiveClient recording every command."""

    instances: list[FakeClient] = []

    def __init__(self, host: str, port: int, username: str, password: str | None, **kwargs: Any) -> None:
        self.host = host
        self.hostname = "AH-7c0680"
        self.paging_disabled = True
        self.connected = True
        self.commands: list[str] = []
        self.responses = _responses()
        self.fail_with: Exception | None = None
        self.dialect = "hiveos"
        FakeClient.instances.append(self)

    async def run(self, command: str, **kwargs: Any) -> str:
        if self.fail_with:
            raise self.fail_with
        self.commands.append(command)
        return self.responses.get(command, "")

    async def run_sequence(self, commands: list[str], **kwargs: Any) -> list[str]:
        from custom_components.aerohive_local.client import AerohiveCommandError
        from custom_components.aerohive_local.parsers import find_cli_error

        outputs = []
        for command in commands:
            out = await self.run(command)
            outputs.append(out)
            if error := find_cli_error(out):
                raise AerohiveCommandError(command, error)
        return outputs

    async def reboot(self, command: str = "reboot") -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.connected = False


@pytest.fixture(autouse=True)
def fake_client():
    FakeClient.instances.clear()
    with (
        patch("custom_components.aerohive_local.AerohiveClient", FakeClient),
        patch("custom_components.aerohive_local.entity.CONTROL_DEBOUNCE_SECONDS", 0.05),
    ):
        yield


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=USER_INPUT, title="AH-7c0680")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _client() -> FakeClient:
    return FakeClient.instances[-1]


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{MAC}_{key}")
    assert entity_id, f"no {platform} entity with key {key}"
    return entity_id


# --------------------------------------------------------------------------
# Config flow
# --------------------------------------------------------------------------


async def _start_manual(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["scan", "manual"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    return result


async def test_config_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await _start_manual(hass)
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AH-7c0680"
    assert result["result"].unique_id == MAC
    assert result["data"][CONF_PASSWORD] == "hunter2-device"


@pytest.mark.parametrize(
    ("error", "expected"),
    [(AerohiveAuthError("no"), "invalid_auth"), (AerohiveConnectionError("down"), "cannot_connect")],
)
async def test_config_flow_errors(hass: HomeAssistant, error: Exception, expected: str) -> None:
    async def failing_run(self, command, **kwargs):  # noqa: ANN001
        raise error

    result = await _start_manual(hass)
    with patch.object(FakeClient, "run", failing_run):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_config_flow_requires_password_or_key(hass: HomeAssistant) -> None:
    result = await _start_manual(hass)
    data = {k: v for k, v in USER_INPUT.items() if k != CONF_PASSWORD}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["errors"] == {"base": "password_or_key"}


# --------------------------------------------------------------------------
# Setup / entities
# --------------------------------------------------------------------------


async def test_setup_device_and_entities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    assert device is not None
    assert device.model == "AP460C"
    assert device.sw_version == "IQ Engine 10.5r3 build-278650"
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections

    assert hass.states.get(_entity_id(hass, "sensor", "clients")).state == "3"
    assert hass.states.get(_entity_id(hass, "sensor", "ssid_clients_AP460C")).state == "3"
    assert hass.states.get(_entity_id(hass, "sensor", "wifi0_clients")).state == "2"
    assert hass.states.get(_entity_id(hass, "sensor", "wifi1_clients")).state == "1"
    assert hass.states.get(_entity_id(hass, "sensor", "wifi0_channel")).state == "6"
    assert hass.states.get(_entity_id(hass, "sensor", "wifi0_noise_floor")).state == "-95.0"
    assert hass.states.get(_entity_id(hass, "sensor", "wifi0_total_utilization")).state == "22.0"
    assert hass.states.get(_entity_id(hass, "sensor", "firmware")).state == "IQ Engine 10.5r3 build-278650"
    assert hass.states.get(_entity_id(hass, "sensor", "region_code")).state == "FCC"
    assert hass.states.get(_entity_id(hass, "sensor", "cpu_usage")).state == "5.94"
    assert hass.states.get(_entity_id(hass, "sensor", "memory_usage")).state == "42.8"
    assert hass.states.get(_entity_id(hass, "sensor", "ip_address")).state == "192.168.1.10"
    assert hass.states.get(_entity_id(hass, "sensor", "serial_number")).state == "01234567890123"
    assert hass.states.get(_entity_id(hass, "sensor", "ssids_broadcasting")).state == "1"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "ssid_broadcasting_AP460C")).state == "on"
    assert hass.states.get(_entity_id(hass, "sensor", "iface_eth1_link_speed")).state == "1000"
    assert hass.states.get(_entity_id(hass, "sensor", "iface_eth1_duplex")).state == "full-duplex"
    clients = hass.states.get(_entity_id(hass, "sensor", "clients")).attributes["clients"]
    assert {c["mac"] for c in clients} == {"e8:b1:fc:5a:1b:2c", "a4:c3:f0:aa:00:01", "3c:22:fb:11:22:33"}
    assert next(c for c in clients if c["mac"] == "3c:22:fb:11:22:33")["band"] == "5 GHz"
    # Throughput needs two polls: unknown after the first one.
    assert hass.states.get(_entity_id(hass, "sensor", "wireless_rx_throughput")).state == "unknown"
    # Raw "every field" sensors exist but are disabled by default.
    raw = er.async_get(hass).async_get(_entity_id(hass, "sensor", "raw_if_wifi0_mtu"))
    assert raw.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    assert device.serial_number == "01234567890123"
    assert device.configuration_url == "https://192.168.1.10"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "iface_eth0")).state == "off"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "iface_eth1")).state == "on"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "iface_wifi2")).state == "off"
    assert hass.states.get(_entity_id(hass, "switch", "ssid_wifi0_AP460C")).state == "on"
    assert hass.states.get(_entity_id(hass, "switch", "ssid_wifi1_AP460C")).state == "on"
    assert hass.states.get(_entity_id(hass, "number", "max_client_AP460C")).state == "50"
    assert _entity_id(hass, "button", "reboot")
    # wifi2 is down and carries no SSID: no switches/sensors for it.
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{MAC}_ssid_wifi2_AP460C") is None
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{MAC}_wifi2_channel") is None
    # No trackers until opted in.
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == "device_tracker"]


async def test_throughput(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    coordinator = entry.runtime_data
    # Pretend the previous poll was 10 s ago, then advance the counters.
    for name, (at, rx, tx) in list(coordinator._counters.items()):  # noqa: SLF001
        coordinator._counters[name] = (at - 10, rx, tx)  # noqa: SLF001
    client.responses["show interface wifi0"] = client.responses["show interface wifi0"].replace(
        "Rx bytes=10000000; Tx bytes=50000000;", "Rx bytes=11250000; Tx bytes=56250000;"
    )
    client.responses["show interface wifi0.1"] = client.responses["show interface wifi0.1"].replace(
        "Rx bytes=400000; Tx bytes=900000;", "Rx bytes=1650000; Tx bytes=7150000;"
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    def kbps(key: str) -> float:
        return float(hass.states.get(_entity_id(hass, "sensor", key)).state)

    # 1.25 MB in 10 s = 1000 kbit/s = 1 Mbit/s (sensors display Mbit/s)
    assert kbps("iface_wifi0_rx_throughput") == pytest.approx(1.0, rel=0.05)
    assert kbps("iface_wifi0_tx_throughput") == pytest.approx(5.0, rel=0.05)
    assert kbps("wireless_rx_throughput") == pytest.approx(1.0, rel=0.05)
    assert kbps("wireless_total_throughput") == pytest.approx(6.0, rel=0.05)
    assert kbps("ssid_rx_throughput_AP460C") == pytest.approx(1.0, rel=0.05)
    assert kbps("wired_rx_throughput") == 0.0


async def test_detailed_stats_off_reduces_commands(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, "detailed_stats": False, "raw_sensors": False}
    )
    await hass.async_block_till_done()
    client = _client()
    client.commands.clear()
    await entry.runtime_data.async_refresh()
    assert "show interface eth1" not in client.commands
    assert "show cpu" not in client.commands
    assert "show interface wifi0" in client.commands  # radio details are always read


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _client().connected is False


async def test_update_failure_marks_unavailable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _client().fail_with = AerohiveConnectionError("gone")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "sensor", "clients")).state == "unavailable"


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------


async def test_switch_off_sends_command_and_saves(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    entity_id = _entity_id(hass, "switch", "ssid_wifi0_AP460C")
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert client.commands[:2] == ["no interface wifi0 ssid AP460C", "save config"]


async def test_switch_debounce(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    entity_id = _entity_id(hass, "switch", "ssid_wifi0_AP460C")
    await asyncio.gather(
        hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True),
        hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True),
        hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True),
    )
    config_cmds = [c for c in client.commands if not c.startswith("show")]
    assert config_cmds == ["no interface wifi0 ssid AP460C", "save config"]


async def test_switch_noop_when_already_in_state(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    entity_id = _entity_id(hass, "switch", "ssid_wifi0_AP460C")
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert [c for c in client.commands if not c.startswith("show")] == []


async def test_device_error_raises(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.responses["no interface wifi0 ssid AP460C"] = "ERROR: cannot remove last SSID"
    entity_id = _entity_id(hass, "switch", "ssid_wifi0_AP460C")
    with pytest.raises(HomeAssistantError, match="cannot remove last SSID"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert "save config" not in client.commands


async def test_number_max_client(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    entity_id = _entity_id(hass, "number", "max_client_AP460C")
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 30}, blocking=True)
    assert client.commands[:2] == ["ssid AP460C max-client 30", "save config"]


async def test_reboot_button(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call("button", "press", {"entity_id": _entity_id(hass, "button", "reboot")}, blocking=True)
    assert "reboot" in _client().commands


async def test_set_password_service_never_logs_key(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client()
    client.commands.clear()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    await hass.services.async_call(
        DOMAIN,
        "set_ssid_password",
        {"device_id": device.id, "ssid": "AP460C", "password": "N3w-S3cret-Key"},
        blocking=True,
    )
    # The security-object comes from the cached running-config summary.
    assert client.commands[:2] == [
        "security-object sec-ap460c security protocol-suite wpa2-aes-psk ascii-key N3w-S3cret-Key",
        "save config",
    ]
    assert "N3w-S3cret-Key" not in caplog.text


async def test_set_password_error_is_redacted(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    cmd = "security-object AP460C security protocol-suite wpa2-aes-psk ascii-key N3w-S3cret-Key"
    client.responses[cmd] = "ERROR: invalid key N3w-S3cret-Key"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_ssid_password",
            {"device_id": device.id, "ssid": "AP460C", "password": "N3w-S3cret-Key", "security_object": "AP460C"},
            blocking=True,
        )
    assert "N3w-S3cret-Key" not in str(err.value)


async def test_set_password_rejects_bad_key(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "set_ssid_password", {"device_id": device.id, "ssid": "AP460C", "password": "short"}, blocking=True
        )


async def test_text_entity_is_write_only(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    entity_id = _entity_id(hass, "text", "psk_AP460C")
    # Disabled by default; enable and reload to exercise it.
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client = _client()
    client.commands.clear()
    await hass.services.async_call("text", "set_value", {"entity_id": entity_id, "value": "AnotherKey99"}, blocking=True)
    assert any(c.endswith("ascii-key AnotherKey99") for c in client.commands)
    assert hass.states.get(entity_id).state == "unknown"


async def test_disconnect_client_service(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    await hass.services.async_call(
        DOMAIN, "disconnect_client", {"device_id": device.id, "mac": "E8:B1:FC:5A:1B:2C"}, blocking=True
    )
    assert client.commands[0] == "clear auth station mac e8b1:fc5a:1b2c"
    assert "save config" not in client.commands


async def test_disconnect_client_fallback(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client()
    client.responses["clear auth station mac e8b1:fc5a:1b2c"] = "    ^-- unknown keyword or invalid input"
    client.commands.clear()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    await hass.services.async_call(
        DOMAIN, "disconnect_client", {"device_id": device.id, "mac": "e8b1:fc5a:1b2c"}, blocking=True
    )
    assert client.commands[:2] == ["clear auth station mac e8b1:fc5a:1b2c", "clear auth station e8b1:fc5a:1b2c"]


async def test_run_show_command_service(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    _client().responses["show running-config"] = "security-object x security protocol-suite wpa2-aes-psk ascii-key abc12345\n"
    response = await hass.services.async_call(
        DOMAIN, "run_show_command", {"device_id": device.id, "command": "show running-config"},
        blocking=True, return_response=True,
    )  # fmt: skip
    assert response["output"].strip().endswith("ascii-key ***")
    response = await hass.services.async_call(
        DOMAIN, "run_show_command", {"device_id": device.id, "command": "show ssid", "redact": False},
        blocking=True, return_response=True,
    )  # fmt: skip
    assert "AP460C" in response["output"]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "run_show_command", {"device_id": device.id, "command": "reboot"},
            blocking=True, return_response=True,
        )  # fmt: skip
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "run_show_command", {"device_id": "nope", "command": "show ssid"},
            blocking=True, return_response=True,
        )  # fmt: skip


# --------------------------------------------------------------------------
# Options / trackers / diagnostics
# --------------------------------------------------------------------------


async def test_options_flow_and_tracker(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 120, CONF_TRACKED_CLIENTS: ["E8B1:FC5A:1B2C", "00:11:22:33:44:55"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRACKED_CLIENTS] == ["e8:b1:fc:5a:1b:2c", "00:11:22:33:44:55"]
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval.total_seconds() == 120
    home = hass.states.get(_entity_id(hass, "device_tracker", "tracker_e8:b1:fc:5a:1b:2c"))
    away = hass.states.get(_entity_id(hass, "device_tracker", "tracker_00:11:22:33:44:55"))
    assert home.state == "home"
    assert home.attributes["ip"] == "192.168.1.50"
    assert home.attributes["ssid"] == "AP460C"
    assert away.state == "not_home"
    signal = hass.states.get(_entity_id(hass, "sensor", "client_e8:b1:fc:5a:1b:2c_signal"))
    assert signal.state == "-45"
    assert hass.states.get(_entity_id(hass, "sensor", "client_e8:b1:fc:5a:1b:2c_tx_rate")).state == "72.0"
    assert hass.states.get(_entity_id(hass, "sensor", "client_00:11:22:33:44:55_signal")).state == "unavailable"


async def test_options_flow_rejects_bad_mac(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_TRACKED_CLIENTS: ["not-a-mac"]}
    )
    assert result["errors"] == {CONF_TRACKED_CLIENTS: "invalid_mac"}


async def test_diagnostics_redacted(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diag)
    assert "hunter2-device" not in text
    assert "192.168.1.10" not in text
    assert "192.168.1.50" not in text
    assert diag["data"]["station_count"] == 3
    assert diag["data"]["version"]["platform"] == "AP460C"


async def test_migration_enables_wifi_key_and_port_switches(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=USER_INPUT, title="AH-7c0680", version=1, minor_version=1)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    psk = registry.async_get_or_create(
        "text", DOMAIN, f"{MAC}_psk_AP460C", config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )  # fmt: skip
    raw = registry.async_get_or_create(
        "sensor", DOMAIN, f"{MAC}_raw_if_wifi0_mtu", config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )  # fmt: skip
    mine = registry.async_get_or_create(
        "switch", DOMAIN, f"{MAC}_port_eth1", config_entry=entry, disabled_by=er.RegistryEntryDisabler.USER
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert registry.async_get(psk.entity_id).disabled_by is None
    assert registry.async_get(raw.entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registry.async_get(mine.entity_id).disabled_by is er.RegistryEntryDisabler.USER
