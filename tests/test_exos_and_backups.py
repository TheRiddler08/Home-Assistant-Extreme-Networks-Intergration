"""ExtremeXOS (Summit X450e) support and power-loss protection (backups, change detection, restore)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import os
from typing import Any
from unittest.mock import patch

import pytest

from .conftest import HAS_HA, load_fixture

if not HAS_HA:
    pytest.skip("Home Assistant test dependencies not installed", allow_module_level=True)

from homeassistant.const import CONF_HOST  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402
from homeassistant.helpers import (  # noqa: E402
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

from custom_components.aerohive_local import backup as backup_mod  # noqa: E402
from custom_components.aerohive_local.const import CONF_TRACKED_CLIENTS, DOMAIN  # noqa: E402

from .test_init import USER_INPUT, FakeClient, entry, fake_client  # noqa: E402, F401

EXOS_MAC = "00:04:96:aa:bb:01"
EXOS_ERROR = "                   ^\n% Invalid input detected at '^' marker."


def _exos_responses() -> dict[str, str]:
    return {
        "show version": load_fixture("synthetic_exos_show_version.txt"),
        "show vlan": load_fixture("synthetic_exos_show_vlan.txt"),
        "show switch": load_fixture("synthetic_exos_show_switch.txt"),
        "show ports no-refresh": load_fixture("synthetic_exos_show_ports.txt"),
        "show ports statistics no-refresh": load_fixture("synthetic_exos_show_ports_statistics.txt"),
        "show inline-power": load_fixture("synthetic_exos_show_inline_power.txt"),
        "show inline-power info ports": load_fixture("synthetic_exos_show_inline_power_info.txt"),
        "show fdb": load_fixture("synthetic_exos_show_fdb.txt"),
        "show cpu-monitoring": load_fixture("synthetic_exos_show_cpu_monitoring.txt"),
        "show memory": load_fixture("synthetic_exos_show_memory.txt"),
        "show configuration": load_fixture("synthetic_exos_show_configuration.txt"),
        "show lldp neighbors": load_fixture("synthetic_exos_show_lldp_neighbors.txt"),
        "show lldp neighbors detailed": load_fixture("synthetic_exos_show_lldp_neighbors_detailed.txt"),
        "show temperature": load_fixture("synthetic_exos_show_temperature.txt"),
        "show fans": load_fixture("synthetic_exos_show_fans.txt"),
        "show power": load_fixture("synthetic_exos_show_power.txt"),
    }


@pytest.fixture
async def exos_entry(hass: HomeAssistant):
    original_init = FakeClient.__init__
    original_run = FakeClient.run

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.hostname = "X450e-48p"
        self.dialect = "exos"
        self.config_unsaved = False
        self.responses = _exos_responses()

    async def run(self, command: str, **kwargs: Any) -> str:
        out = await original_run(self, command, **kwargs)
        if command.startswith("show") and command not in self.responses:
            return EXOS_ERROR
        return out

    with (
        patch.object(FakeClient, "__init__", init),
        patch.object(FakeClient, "run", run),
        patch("custom_components.aerohive_local.coordinator.discovery_flow.async_create_flow") as flows,
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=EXOS_MAC, title="X450e-48p", minor_version=3,
            data={**USER_INPUT, CONF_HOST: "192.168.9.65"},
            options={CONF_TRACKED_CLIENTS: ["b8:a4:4f:00:d3:de", "00:15:5d:00:00:01"]},
        )  # fmt: skip
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.discovery_calls = flows.call_args_list  # type: ignore[attr-defined]
        yield entry


def _x(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{EXOS_MAC}_{key}")
    assert entity_id, f"no {platform} entity {key}"
    return entity_id


def _client() -> FakeClient:
    return FakeClient.instances[-1]


def _config_cmds(client: FakeClient) -> list[str]:
    return [c for c in client.commands if not c.startswith("show")]


# --------------------------------------------------------------------------
# ExtremeXOS switch
# --------------------------------------------------------------------------


async def test_exos_entities(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, EXOS_MAC)})
    assert device.model == "X450e-48p"
    assert device.sw_version == "ExtremeXOS 12.6.3.4"
    st = hass.states.get
    assert st(_x(hass, "binary_sensor", "iface_1")).state == "on"
    assert st(_x(hass, "binary_sensor", "iface_3")).state == "off"
    assert st(_x(hass, "sensor", "iface_1_link_speed")).state == "1000"
    assert st(_x(hass, "sensor", "iface_2_duplex")).state == "Full"
    assert st(_x(hass, "switch", "port_5")).state == "off"  # "disable ports 5"
    assert st(_x(hass, "switch", "port_1")).state == "on"
    assert st(_x(hass, "sensor", "iface_4_poe_power")).state == "15.2"
    assert st(_x(hass, "switch", "poe_3")).state == "off"
    assert st(_x(hass, "switch", "poe_1")).state == "on"
    assert st(_x(hass, "sensor", "poe_power_used")).state == "21.0"
    assert st(_x(hass, "sensor", "poe_power_budget")).state == "370.0"
    assert st(_x(hass, "sensor", "temperature")).state == "43.0"
    assert st(_x(hass, "sensor", "fan_status")).state == "Operational"
    assert st(_x(hass, "sensor", "power_supply_status")).state == "Powered On"
    assert st(_x(hass, "sensor", "boot_count")).state == "42"
    assert st(_x(hass, "sensor", "image_booted")).state == "primary"
    assert st(_x(hass, "sensor", "config_booted")).state == "primary.cfg"
    assert st(_x(hass, "sensor", "system_state")).state == "OPERATIONAL"
    assert st(_x(hass, "sensor", "cpu_usage")).state == "2.0"
    assert st(_x(hass, "binary_sensor", "boot_config_problem")).state == "off"
    assert st(_x(hass, "binary_sensor", "unsaved_config")).state == "off"
    assert st(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "off"
    assert st(_x(hass, "sensor", "last_backup")).state not in ("unknown", "unavailable")
    assert st(_x(hass, "sensor", "fan_speed")).state == "4790"  # the slowest fan
    vlans = st(_x(hass, "sensor", "vlans"))
    assert vlans.state == "3" and vlans.attributes["vlans"][0]["ip_address"] == "192.168.8.20"
    assert st(_x(hass, "sensor", "ip_address")).state == "192.168.8.20"
    assert st(_x(hass, "sensor", "serial_number")).state == "0905G-00001"
    assert device.serial_number == "0905G-00001"
    # Port 49 leads to the core switch (LLDP): its MACs aren't wired clients of this switch.
    assert st(_x(hass, "sensor", "wired_clients")).state == "2"
    assert st(_x(hass, "switch", "port_49")).attributes["uplink"] is True
    assert st(_x(hass, "device_tracker", "tracker_b8:a4:4f:00:d3:de")).state == "home"
    assert st(_x(hass, "device_tracker", "tracker_00:15:5d:00:00:01")).state == "not_home"
    lldp = st(_x(hass, "sensor", "iface_2_lldp_neighbor"))
    assert lldp.state == "AH-7c0680" and lldp.attributes["management_address"] == "192.168.9.73"
    # The unconfigured AP behind port 2 and the Extreme core switch behind port 49 are offered for setup.
    calls = exos_entry.discovery_calls  # type: ignore[attr-defined]
    assert sorted(c.kwargs["data"][CONF_HOST] for c in calls) == ["192.168.8.2", "192.168.9.73"]
    # No Wi-Fi entities on a switch.
    ents = er.async_entries_for_config_entry(er.async_get(hass), exos_entry.entry_id)
    assert not [e for e in ents if "ssid" in e.unique_id or "wifi" in e.unique_id]


async def test_exos_controls(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    client.commands.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": _x(hass, "switch", "poe_4")}, blocking=True)
    assert _config_cmds(client)[:2] == ["disable inline-power ports 4", "save configuration"]

    client.commands.clear()
    await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "poe_cycle_1")}, blocking=True)
    assert _config_cmds(client) == ["reset inline-power ports 1"]

    client.commands.clear()
    await hass.services.async_call("switch", "turn_on", {"entity_id": _x(hass, "switch", "port_5")}, blocking=True)
    assert _config_cmds(client)[:2] == ["enable ports 5", "save configuration"]

    with pytest.raises(HomeAssistantError, match="uplink"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": _x(hass, "switch", "port_49")}, blocking=True)

    client.commands.clear()
    await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "reboot")}, blocking=True)
    assert client.commands == ["reboot"]


async def test_exos_services_and_safe_reboot(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, EXOS_MAC)})
    client.commands.clear()
    await hass.services.async_call(
        DOMAIN, "set_port_description", {"device_id": device.id, "port": "4", "description": "cam-1"}, blocking=True
    )
    assert _config_cmds(client)[:2] == ["configure ports 4 display-string cam-1", "save configuration"]
    client.commands.clear()
    await hass.services.async_call(DOMAIN, "set_port_description", {"device_id": device.id, "port": "4"}, blocking=True)
    assert _config_cmds(client)[0] == "unconfigure ports 4 display-string"
    with pytest.raises(HomeAssistantError, match="15"):
        await hass.services.async_call(
            DOMAIN, "set_port_description", {"device_id": device.id, "port": "4", "description": "has space"},
            blocking=True,
        )  # fmt: skip
    client.commands.clear()
    await hass.services.async_call(DOMAIN, "set_poe", {"device_id": device.id, "port": "6", "enabled": False}, blocking=True)
    assert _config_cmds(client)[0] == "disable inline-power ports 6"

    # Unsaved changes: the reboot button refuses; the action needs save_changes.
    client.config_unsaved = True
    client.commands.clear()
    with pytest.raises(HomeAssistantError, match="unsaved configuration changes"):
        await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "reboot")}, blocking=True)
    with pytest.raises(HomeAssistantError, match="confirm"):
        await hass.services.async_call(DOMAIN, "reboot", {"device_id": device.id, "confirm": False}, blocking=True)
    assert client.commands == []
    await hass.services.async_call(
        DOMAIN, "reboot", {"device_id": device.id, "confirm": True, "save_changes": True}, blocking=True
    )
    assert client.commands == ["reboot (save)"]
    client.commands.clear()
    await hass.services.async_call(
        DOMAIN, "reboot", {"device_id": device.id, "confirm": True, "save_changes": False}, blocking=True
    )
    assert client.commands == ["reboot"]


async def test_exos_poe_cycle_fallback(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    client.responses["reset inline-power ports 1"] = EXOS_ERROR
    client.commands.clear()
    with patch("custom_components.aerohive_local.coordinator.asyncio.sleep", return_value=None):
        await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "poe_cycle_1")}, blocking=True)
    assert _config_cmds(client) == [
        "reset inline-power ports 1", "disable inline-power ports 1", "enable inline-power ports 1",
    ]  # fmt: skip


async def test_exos_unsaved_and_boot_problem(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    coordinator = exos_entry.runtime_data
    client.config_unsaved = True
    client.responses["show switch"] = (
        load_fixture("synthetic_exos_show_switch.txt")
        .replace("Config Booted:    primary.cfg", "Config Booted:    Factory Default")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_x(hass, "binary_sensor", "unsaved_config")).state == "on"
    problem = hass.states.get(_x(hass, "binary_sensor", "boot_config_problem"))
    assert problem.state == "on" and "Factory Default" in problem.attributes["problems"][0]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"boot_config_problem_{exos_entry.entry_id}")
    assert issue is not None and issue.translation_key == "boot_config_problem"

    # Save configuration clears the '*' marker; a good boot clears the issue.
    client.config_unsaved = False
    client.responses["show switch"] = load_fixture("synthetic_exos_show_switch.txt")
    client.commands.clear()
    await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "save_config")}, blocking=True)
    assert "save configuration" in client.commands
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_x(hass, "binary_sensor", "unsaved_config")).state == "off"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"boot_config_problem_{exos_entry.entry_id}") is None


# --------------------------------------------------------------------------
# Backups and change detection
# --------------------------------------------------------------------------


def _backups(entry: MockConfigEntry) -> list[backup_mod.BackupFile]:
    return backup_mod.list_backups(entry.runtime_data.backup_folder)


def _age_backups(entry: MockConfigEntry, hours: int = 2) -> None:
    """Pretend the existing backups were taken a while ago."""
    for backup in _backups(entry):
        old = (backup.created - timedelta(hours=hours)).strftime("%Y%m%d-%H%M%S")
        os.rename(backup.path, backup.path.with_name(f"{old}.cfg"))


async def _poll_config(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data
    coordinator._config_dirty = True  # noqa: SLF001
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_first_backup_and_external_change(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    backups = _backups(exos_entry)
    assert len(backups) == 1
    assert "disable ports 5" in backup_mod.read_backup(backups[0].path)
    # Unchanged config (only a comment differs): no new file.
    client = _client()
    client.responses["show configuration"] = "# generated at a different time\n" + client.responses["show configuration"]
    await _poll_config(hass, exos_entry)
    assert len(_backups(exos_entry)) == 1
    # Someone changed the config on the CLI while the switch kept running: backed up.
    _age_backups(exos_entry)
    client.responses["show configuration"] += "configure ports 7 display-string printer\n"
    await _poll_config(hass, exos_entry)
    assert len(_backups(exos_entry)) == 2
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "off"


async def test_change_after_reboot_is_flagged_then_accepted(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    _age_backups(exos_entry)
    # Power cut: the switch rebooted 1 minute ago and lost "disable ports 5".
    client.responses["show switch"] = load_fixture("synthetic_exos_show_switch.txt").replace(
        "System UpTime:    6 hours 0 minutes 12 seconds", "System UpTime:    0 hours 1 minutes 0 seconds"
    )
    client.responses["show configuration"] = client.responses["show configuration"].replace("disable ports 5\n", "")
    await _poll_config(hass, exos_entry)

    flag = hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot"))
    assert flag.state == "on"
    assert flag.attributes["lines_removed"] == 1
    assert "- disable ports 5" in flag.attributes["changes"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"config_changed_after_reboot_{exos_entry.entry_id}")
    assert issue is not None and issue.translation_placeholders["removed"] == "1"
    assert len(_backups(exos_entry)) == 1  # known-good backup kept as the latest

    # An unrelated change from Home Assistant doesn't hide the problem.
    await hass.services.async_call("switch", "turn_off", {"entity_id": _x(hass, "switch", "poe_4")}, blocking=True)
    await _poll_config(hass, exos_entry)
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "on"
    assert len(_backups(exos_entry)) == 1

    # "Back up now" is refused so the known-good backup isn't replaced by accident.
    with pytest.raises(HomeAssistantError, match="changed after a reboot"):
        await hass.services.async_call(
            "button", "press", {"entity_id": _x(hass, "button", "backup_config")}, blocking=True
        )

    # The user decides the current configuration is right.
    await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "accept_config")}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "off"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"config_changed_after_reboot_{exos_entry.entry_id}") is None
    assert len(_backups(exos_entry)) == 2


async def test_restore_brings_back_the_backup(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, EXOS_MAC)})
    _age_backups(exos_entry)
    client.responses["show switch"] = load_fixture("synthetic_exos_show_switch.txt").replace(
        "6 hours 0 minutes 12 seconds", "0 hours 1 minutes 0 seconds"
    )
    good_config = client.responses["show configuration"]
    client.responses["show configuration"] = good_config.replace("disable ports 5\n", "")
    await _poll_config(hass, exos_entry)
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "on"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "restore_config", {"device_id": device.id, "confirm": False}, blocking=True, return_response=True
        )

    listing = await hass.services.async_call(
        DOMAIN, "list_backups", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert len(listing["backups"]) == 1

    client.commands.clear()
    client.responses["configure ports 49 display-string uplink"] = "Error: something odd"
    result = await hass.services.async_call(
        DOMAIN, "restore_config", {"device_id": device.id, "confirm": True, "save": True},
        blocking=True, return_response=True,
    )  # fmt: skip
    sent = _config_cmds(client)
    assert sent[0] == 'configure snmp sysName "X450e-48p"'
    assert "disable ports 5" in sent and not any(c.startswith("#") for c in sent)
    assert sent[-1] == "save configuration"
    assert result["lines_sent"] == 9 and result["errors"] == 1 and result["saved"] is True

    # The device now reports the restored configuration: the problem clears.
    client.responses["show configuration"] = good_config
    await _poll_config(hass, exos_entry)
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "off"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"config_changed_after_reboot_{exos_entry.entry_id}") is None


async def test_cut_off_config_read_is_not_backed_up(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    coordinator = exos_entry.runtime_data
    full = client.responses["show configuration"]
    reads = iter([full[:60], full])  # first read cut off, the confirming read is complete
    original_run = FakeClient.run

    async def flaky(self, command: str, **kwargs: Any) -> str:
        if command == "show configuration":
            self.commands.append(command)
            return next(reads, full)
        return await original_run(self, command, **kwargs)

    _age_backups(exos_entry)
    with patch.object(FakeClient, "run", flaky):
        coordinator._config_dirty = True  # noqa: SLF001
        await coordinator.async_refresh()
    assert len(_backups(exos_entry)) == 1  # nothing written for the cut-off read
    assert hass.states.get(_x(hass, "binary_sensor", "config_changed_after_reboot")).state == "off"


async def test_backup_service_and_hiveos_backups(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, EXOS_MAC)})
    result = await hass.services.async_call(
        DOMAIN, "backup_config", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert result["file"].endswith(".cfg")
    assert len(_backups(exos_entry)) == 1  # unchanged: no duplicate
    last = hass.states.get(_x(hass, "sensor", "last_backup"))
    assert last.attributes["backups"] == 1
    assert last.attributes["folder"].endswith(f"X450e-48p_{EXOS_MAC.replace(':', '')}")


def test_restore_lines_per_dialect() -> None:
    exos = backup_mod.restore_lines("#\n# Module x\nenable sntp-client\n\ndisable ports 5\n", "exos")
    assert exos == ["enable sntp-client", "disable ports 5"]
    hiveos = backup_mod.restore_lines(
        "ssid A\nsecurity-object A security protocol-suite wpa2-aes-psk ascii-key ***\n", "hiveos"
    )
    assert hiveos == ["ssid A"]  # masked key lines can't be restored
    switch = backup_mod.restore_lines("!Current Configuration:\nconfigure\ninterface 1/0/3\nno poe\nexit\n", "switch")
    assert switch == ["configure", "interface 1/0/3", "no poe", "exit"]


def test_backup_files(tmp_path) -> None:
    now = dt_util.utcnow()
    first, written = backup_mod.write_backup(tmp_path, "a\nb\n", now)
    assert written
    same, written = backup_mod.write_backup(tmp_path, "# comment\na\nb\n", now + timedelta(minutes=1))
    assert not written and same.path == first.path
    for i in range(25):
        backup_mod.write_backup(tmp_path, f"a\nline {i}\n", now + timedelta(minutes=2 + i))
    assert len(backup_mod.list_backups(tmp_path)) == backup_mod.BACKUPS_TO_KEEP
    summary = backup_mod.diff_summary("a\nb\nc\n", "a\nc\nd\n")
    assert summary["added"] == 1 and summary["removed"] == 1
    assert summary["sample"] == ["- b", "+ d"]


async def test_config_flow_exos_uses_system_mac(hass: HomeAssistant) -> None:
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    original = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.hostname = "X450e-48p"
        self.dialect = "exos"
        self.responses = _exos_responses()

    with patch.object(FakeClient, "__init__", init):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
        with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.9.65"}
            )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "X450e-48p"
    assert result["result"].unique_id == EXOS_MAC
    assert "show interface" not in FakeClient.instances[-1].commands


async def test_overview_and_diagnostics_report_problems(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    from custom_components.aerohive_local.diagnostics import async_get_config_entry_diagnostics

    client = _client()
    client.config_unsaved = True
    await exos_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    overview = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("entry_type") == "overview")
    await overview.runtime_data.async_refresh()
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "overview_config_problems")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["devices"] == [{"name": "X450e-48p", "problems": ["unsaved configuration changes"]}]
    diag = await async_get_config_entry_diagnostics(hass, exos_entry)
    assert diag["data"]["cli"] == "exos"
    assert diag["data"]["config_unsaved"] is True
    assert diag["data"]["backup"]["count"] == 1
    assert "00:04:96:aa:bb:01" not in repr(diag)


def test_diagnostics_redactor() -> None:
    from custom_components.aerohive_local.diagnostics import redact

    data = {
        "raw": {"system_mac": "00:04:96:aa:bb:01", "machine_type": "X450e-48p", "wifi0_mac_addr": "bcf3:107c:0690",
                "serial_number": "123", "ip_addr": "10.0.0.1", "mtu": "1500"},
        "stations": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5", "hostname": "phone", "signal": -50}],
        "lldp": {"chassis_id": "00:04:96:12:34:56", "management_address": "10.0.0.2", "port_id": "1:24"},
        "empty_mac": None,
    }  # fmt: skip
    out = redact(data)
    assert out["raw"] == {"system_mac": "**REDACTED**", "machine_type": "X450e-48p", "wifi0_mac_addr": "**REDACTED**",
                          "serial_number": "**REDACTED**", "ip_addr": "**REDACTED**", "mtu": "1500"}  # fmt: skip
    assert out["stations"][0] == {"mac": "**REDACTED**", "ip": "**REDACTED**", "hostname": "**REDACTED**", "signal": -50}
    assert out["lldp"]["chassis_id"] == "**REDACTED**" and out["lldp"]["port_id"] == "1:24"
    assert out["empty_mac"] is None


# --------------------------------------------------------------------------
# Network panel: WebSocket snapshots
# --------------------------------------------------------------------------


async def test_websocket_snapshot_and_subscribe(hass: HomeAssistant, hass_ws_client, entry: MockConfigEntry, exos_entry: MockConfigEntry) -> None:  # noqa: F811
    import json

    from custom_components.aerohive_local.websocket_api import SIGNAL_SNAPSHOT
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "aerohive_local/snapshot"})
    msg = await client.receive_json()
    assert msg["success"]
    snap = msg["result"]
    kinds = {d["name"]: d["kind"] for d in snap["devices"]}
    assert kinds == {"X450e-48p": "switch", "AH-7c0680": "ap"}
    switch = snap["devices"][0]
    assert switch["name"] == "X450e-48p" and switch["online"] and switch["device_id"]
    ports = {p["name"]: p for p in switch["ports"]}
    assert len(ports) == 10 and ports["4"]["label"] == "camera"
    assert ports["1"]["up"] and ports["1"]["speed"] == 1000
    assert ports["5"]["enabled"] is False and ports["50"]["present"] is False
    assert ports["4"]["poe"]["power_w"] == 15.2 and ports["4"]["poe"]["delivering"]
    assert ports["2"]["neighbor"]["name"] == "AH-7c0680" and ports["2"]["neighbor"]["ap"]
    assert ports["49"]["uplink"]
    assert switch["vlans"][0]["ip"] == "192.168.8.20" and switch["ip"] == "192.168.8.20"
    assert switch["features"]["port_names"] and switch["config"]["backups"] == 1
    ap = snap["devices"][1]
    assert ap["radios"] and ap["ssids"] and not ap["ports"]
    assert "password" not in json.dumps(snap).lower()
    if out := os.environ.get("AEROHIVE_SNAPSHOT_OUT"):
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(snap, handle, indent=1)

    await client.send_json({"id": 2, "type": "aerohive_local/subscribe"})
    assert (await client.receive_json())["success"]
    first = await client.receive_json()
    assert first["type"] == "event" and len(first["event"]["devices"]) == 2
    # Updates are coalesced into one event.
    with patch("custom_components.aerohive_local.websocket_api.COALESCE_SECONDS", 0):
        async_dispatcher_send(hass, SIGNAL_SNAPSHOT)
        async_dispatcher_send(hass, SIGNAL_SNAPSHOT)
        await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    update = await client.receive_json()
    assert update["id"] == 2 and update["event"]["devices"]


async def test_controls_show_new_state_instantly_and_save_once(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    """A change shows at once, can be undone straight away, and a burst is saved once in the background."""
    client = _client()
    coordinator = exos_entry.runtime_data
    port5 = _x(hass, "switch", "port_5")
    assert hass.states.get(port5).state == "off"
    client.commands.clear()
    with (
        patch("custom_components.aerohive_local.coordinator.SAVE_DELAY_SECONDS", 0.3),
        patch.object(coordinator, "async_request_refresh") as refresh,
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": port5}, blocking=True)
        # No poll has run, yet the switch shows the state the device accepted.
        assert hass.states.get(port5).state == "on"
        assert coordinator.data.port_enabled("5") is True
        assert _config_cmds(client) == ["enable ports 5"]
        # Turning it straight back off sends the command (it used to be skipped as "already off").
        await hass.services.async_call("switch", "turn_off", {"entity_id": port5}, blocking=True)
        assert hass.states.get(port5).state == "off"
        assert _config_cmds(client) == ["enable ports 5", "disable ports 5"]
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await asyncio.sleep(0.5)
        await hass.async_block_till_done()
        assert _config_cmds(client) == ["enable ports 5", "disable ports 5", "save configuration"]
        assert refresh.called

    # A poll that was already running when a change was made doesn't undo it on screen.
    original_run = client.run

    async def run(command: str, **kwargs: Any) -> str:
        if command == "show ports no-refresh":
            coordinator._set_overrides({("port", "3"): False})  # noqa: SLF001 - user turns port 3 off mid-poll
        return await original_run(command, **kwargs)

    with patch.object(client, "run", run):
        await coordinator.async_refresh()
    assert coordinator.data.port_enabled("3") is False
    # The next poll reads the device's real state again.
    await coordinator.async_refresh()
    assert coordinator.data.port_enabled("3") is True


async def test_failed_background_save_is_reported_and_retried(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    client = _client()
    coordinator = exos_entry.runtime_data
    client.responses["save configuration"] = "Error: Unable to save configuration."
    with patch("custom_components.aerohive_local.coordinator.SAVE_DELAY_SECONDS", 0.05):
        await hass.services.async_call("switch", "turn_on", {"entity_id": _x(hass, "switch", "port_5")}, blocking=True)
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        await coordinator.async_refresh()
        assert coordinator.data.save_error and "Unable to save" in coordinator.data.save_error
        client.responses["save configuration"] = ""
        client.commands.clear()
        await coordinator.async_refresh()  # retries the save
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
    assert "save configuration" in client.commands
    await coordinator.async_refresh()
    assert coordinator.data.save_error is None


async def test_refresh_now_reads_everything(hass: HomeAssistant, entry: MockConfigEntry, exos_entry: MockConfigEntry) -> None:  # noqa: F811
    switch_client = _client()
    switch_client.commands.clear()
    await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "refresh")}, blocking=True)
    # Also the slow data (LLDP, health, VLANs) and the configuration, not just the fast poll.
    for command in ("show switch", "show lldp neighbors", "show temperature", "show vlan", "show configuration"):
        assert command in switch_client.commands, command
    # The action without a device refreshes every device.
    ap_client = FakeClient.instances[0]
    ap_client.commands.clear()
    switch_client.commands.clear()
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    assert "show version" in ap_client.commands and "show switch" in switch_client.commands
    # A device that can't be reached says so, and the button stays usable to retry.
    from custom_components.aerohive_local.client import AerohiveConnectionError

    switch_client.fail_with = AerohiveConnectionError("unreachable")
    with pytest.raises(HomeAssistantError, match="Could not read"):
        await hass.services.async_call("button", "press", {"entity_id": _x(hass, "button", "refresh")}, blocking=True)
    assert hass.states.get(_x(hass, "button", "refresh")).state != "unavailable"


async def test_switch_with_unrecognised_prompt_is_still_a_switch(hass: HomeAssistant) -> None:
    """The prompt decides the CLI first; if it fooled us, 'show version' sets it right."""
    from custom_components.aerohive_local.snapshot import device_kind

    original_init = FakeClient.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.hostname = "X450e-48p"
        self.dialect = "hiveos"  # e.g. a prompt that looked like an access point's
        self.config_unsaved = False
        self.responses = _exos_responses()

    with patch.object(FakeClient, "__init__", init):
        entry = MockConfigEntry(domain=DOMAIN, unique_id=EXOS_MAC, title="X450e-48p", minor_version=3, data=USER_INPUT)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    data = entry.runtime_data.data
    assert data.cli == "exos" and data.is_switch and device_kind(data) == "switch"
    assert len(data.ports()) == 10
    assert _x(hass, "switch", "port_5")


def test_unrecognised_device_is_not_called_an_access_point() -> None:
    from custom_components.aerohive_local.coordinator import AerohiveData
    from custom_components.aerohive_local.snapshot import device_kind

    assert device_kind(AerohiveData()) == "unknown"


async def test_scan_shows_what_other_devices_know(hass: HomeAssistant, exos_entry: MockConfigEntry) -> None:
    """Found devices are labelled with LLDP names and the switch port they're on."""
    from custom_components.aerohive_local.discovery import ScanResult

    from .test_discovery_and_switch import _scan_flow

    found = [
        ScanResult(host="192.168.9.73", banner="SSH-2.0-OpenSSH_5.2"),  # the AP behind port 2 (LLDP)
        ScanResult(host="192.168.8.61", banner="SSH-2.0-dropbear", mac="b8:a4:4f:00:d3:de"),  # learned on port 4
        ScanResult(host="192.168.9.65", banner="SSH-2.0-OpenSSH_5.2"),  # the switch itself, already added
    ]
    result = await _scan_flow(hass, found)
    labels = {o["value"]: o["label"] for o in result["data_schema"].schema["host"].config["options"]}
    assert labels["192.168.9.73"].startswith("192.168.9.73 · AH-7c0680") and "on X450e-48p port 2" in labels["192.168.9.73"]
    assert "on X450e-48p port 4" in labels["192.168.8.61"] and "b8:a4:4f:00:d3:de" in labels["192.168.8.61"]
    assert "(already added)" in labels["192.168.9.65"]
