"""UniFi access points and switches through the UniFi Network controller: parsing, polling, controls, the add flow."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.aerohive_local import unifi as uf

from .conftest import HAS_HA, load_fixture


def fx(name: str) -> Any:
    return json.loads(load_fixture(f"unifi/{name}"))["data"]


def test_parsers() -> None:
    devices = uf.parse_devices(fx("stat_device.json"))
    ap, sw = devices["f4:92:bf:00:00:01"], devices["74:83:c2:00:00:02"]
    assert (ap.kind, ap.model, ap.connected, ap.cpu, ap.memory) == (
        "ap",
        "U6LR",
        True,
        3.1,
        41.5,
    )
    assert [(r.name, r.band, r.channel, r.width, r.clients) for r in ap.radios] == [
        ("wifi0", "2.4 GHz", 6, 20, 2),
        ("wifi1", "5 GHz", 44, 80, 1),
    ]
    assert [(v.essid, v.radio) for v in ap.vaps] == [
        ("home", "wifi0"),
        ("home", "wifi1"),
        ("Guest", "wifi1"),
    ]
    assert (sw.kind, sw.temperature, sw.poe_budget) == ("switch", 41.5, 60.0)
    camera = sw.ports[1]
    assert (
        camera.index,
        camera.name,
        camera.speed,
        camera.poe_capable,
        camera.poe_on,
        camera.poe_power,
    ) == (2, "Camera", 100, True, True, 4.2)
    assert (
        sw.ports[2].poe_on is False
        and sw.ports[0].poe_on is None
        and sw.ports[0].uplink
    )
    assert (
        devices["70:a7:41:00:00:03"].kind == "router"
        and not devices["f4:92:bf:00:00:04"].connected
    )

    clients = uf.parse_clients(fx("stat_sta.json"))
    assert (clients[1].signal, clients[1].name) == (
        -67,
        "Kitchen tablet",
    )  # rssi when there's no signal
    assert (clients[2].wired, clients[2].sw_port, clients[2].vlan) == (True, 2, 30)
    wlans = uf.parse_wlans(fx("wlanconf.json"))
    assert (wlans["home"].bands, wlans["Guest"].hidden, wlans["Guest"].bands) == (
        ["2g", "5g"],
        True,
        ["5g"],
    )
    assert uf.rate_text(866700) == "867M" and uf.rate_text(None) is None

    overrides = uf.merged_overrides(
        [{"port_idx": 2, "name": "Camera"}], 2, {"poe_mode": "off"}
    )
    assert overrides == [{"port_idx": 2, "name": "Camera", "poe_mode": "off"}]
    assert uf.merged_overrides([], 5, {"poe_mode": "off"}) == [
        {"port_idx": 5, "poe_mode": "off"}
    ]
    assert uf.parse_devices([{"mac": "nope"}, "junk"]) == {}


if HAS_HA:
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.exceptions import HomeAssistantError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local.const import DOMAIN
    from custom_components.aerohive_local.snapshot import network_snapshot

needs_ha = pytest.mark.skipif(
    not HAS_HA, reason="needs pytest-homeassistant-custom-component"
)

BASE = {
    "device_type": "unifi",
    "host": "192.168.1.1",
    "port": 443,
    "username": "ha",
    "password": "pw",
    "site": "default",
    "verify_ssl": False,
}


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.refuse = False

    async def request(
        self,
        controller: uf.UniFiController,
        method: str,
        path: str,
        payload: Any = None,
    ) -> Any:
        self.calls.append((method, path, payload))
        if self.refuse:
            raise uf.UniFiAuthError("refused")
        if method == "GET":
            return fx(
                {
                    "stat/device": "stat_device.json",
                    "stat/sta": "stat_sta.json",
                    "rest/wlanconf": "wlanconf.json",
                }[path]
            )
        return []


@pytest.fixture
def controller():
    fake = FakeController()
    uf._CONTROLLERS.clear()

    async def request(self, method, path, payload=None):
        return await fake.request(self, method, path, payload)

    with patch.object(uf.UniFiController, "request", request):
        yield fake


async def _add(hass: HomeAssistant, mac: str, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=mac,
        title=title,
        minor_version=3,
        data={**BASE, "device_mac": mac},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@needs_ha
async def test_unifi_ap_and_switch_are_read(hass: HomeAssistant, controller) -> None:
    ap = await _add(hass, "f4:92:bf:00:00:01", "Living room AP")
    sw = await _add(hass, "74:83:c2:00:00:02", "Office switch")
    snap = {d["entry_id"]: d for d in network_snapshot(hass)["devices"]}
    a, s = snap[ap.entry_id], snap[sw.entry_id]
    assert a["kind"] == "ap" and s["kind"] == "switch"
    assert sorted(r["name"] for r in a["radios"]) == ["wifi0", "wifi1"]
    assert {x["name"] for x in a["ssids"]} == {"home", "Guest"}
    data = ap.runtime_data.data
    assert (
        data.stations["02:11:22:33:44:01"].signal == -52
        and data.stations["02:11:22:33:44:01"].tx_rate == "867M"
    )
    assert "02:11:22:33:44:09" not in data.stations  # on another AP
    assert data.ssid_on_radio("Guest", "wifi1") and not data.ssid_on_radio(
        "Guest", "wifi0"
    )
    ports = {p["name"]: p for p in s["ports"]}
    assert (
        ports["2"]["poe"]["power_w"] == 4.2
        and ports["3"]["poe"]["enabled"] is False
        and ports["1"]["uplink"]
    )
    assert sw.runtime_data.data.port_clients("2")[0].mac == "bc:ad:28:00:00:05"
    assert (
        s["features"]["port_control"]
        and not s["features"]["backup"]
        and not s["features"]["save"]
    )
    # Both entries share one controller session and (within a few seconds) one device list.
    assert sum(1 for c in controller.calls if c[1] == "stat/device") == 1


@needs_ha
async def test_unifi_controls(hass: HomeAssistant, controller) -> None:
    ap = await _add(hass, "f4:92:bf:00:00:01", "Living room AP")
    sw = await _add(hass, "74:83:c2:00:00:02", "Office switch")
    switch, access = sw.runtime_data, ap.runtime_data

    def last(method: str) -> tuple[str, str, Any]:
        return next(c for c in reversed(controller.calls) if c[0] == method)

    await switch.async_set_poe("3", True)
    assert last("PUT")[:2] == ("PUT", "rest/device/64aa00000000000000000002")
    assert {"port_idx": 3, "poe_mode": "auto"} in last("PUT")[2]["port_overrides"]
    assert {"port_idx": 2, "name": "Camera", "poe_mode": "auto"} in last("PUT")[2][
        "port_overrides"
    ]  # others kept

    await switch.async_set_port_enabled("3", False)
    assert {"port_idx": 3, "forward": "disabled"} in last("PUT")[2]["port_overrides"]
    with pytest.raises(HomeAssistantError, match="uplink"):
        await switch.async_set_port_enabled("1", False)

    await switch.async_power_cycle_poe("2")
    assert next(c for c in reversed(controller.calls) if c[0] == "POST") == (
        "POST",
        "cmd/devmgr",
        {"cmd": "power-cycle", "mac": "74:83:c2:00:00:02", "port_idx": 2},
    )

    await access.async_set_ssid_password("home", "new-pass-123")
    assert next(c for c in reversed(controller.calls) if c[0] == "PUT") == (
        "PUT",
        "rest/wlanconf/w1",
        {"x_passphrase": "new-pass-123"},
    )
    await access.async_set_ssid_all_radios("Guest", False)
    assert next(c for c in reversed(controller.calls) if c[0] == "PUT") == (
        "PUT",
        "rest/wlanconf/w2",
        {"enabled": False},
    )
    assert (
        await access.async_update_ssid("Guest", hidden=False, radios=["wifi1"]) is True
    )
    assert next(c for c in reversed(controller.calls) if c[0] == "PUT")[2] == {
        "hide_ssid": False,
        "enabled": True,
    }
    with pytest.raises(HomeAssistantError, match="all access points"):
        await access.async_set_ssid_on_radio("wifi0", "home", False)

    await access.async_disconnect_client("02:11:22:33:44:01")
    assert next(c for c in reversed(controller.calls) if c[0] == "POST") == (
        "POST",
        "cmd/stamgr",
        {"cmd": "kick-sta", "mac": "02:11:22:33:44:01"},
    )
    await access.async_reboot()
    assert next(c for c in reversed(controller.calls) if c[0] == "POST") == (
        "POST",
        "cmd/devmgr",
        {"cmd": "restart", "mac": "f4:92:bf:00:00:01"},
    )


@needs_ha
async def test_unifi_add_flow(hass: HomeAssistant, controller) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "unifi"}
    )
    assert result["step_id"] == "unifi"
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.1", "username": "ha", "password": "pw"},
        )
        assert result["step_id"] == "unifi_devices"
        options = (
            result["data_schema"].schema["devices"].container
            if hasattr(result["data_schema"].schema["devices"], "container")
            else None
        )
        if options is not None:
            assert set(options) == {
                "f4:92:bf:00:00:01",
                "74:83:c2:00:00:02",
                "f4:92:bf:00:00:04",
            }  # no gateway
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"devices": ["f4:92:bf:00:00:01", "74:83:c2:00:00:02"]}
        )
        assert (
            result["type"] is FlowResultType.CREATE_ENTRY
            and result["data"]["device_mac"] == "f4:92:bf:00:00:01"
        )
        await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert {e.unique_id for e in entries} == {"f4:92:bf:00:00:01", "74:83:c2:00:00:02"}
    assert {e.title for e in entries} == {"Living room AP", "Office switch"}

    controller.refuse = True
    uf._CONTROLLERS.clear()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "unifi"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.1", "username": "ha", "password": "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
