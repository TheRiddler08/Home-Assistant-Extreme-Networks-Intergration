"""Wi-Fi networks managed from the Network panel: WebSocket commands, timed changes, the set_ssid action."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from .conftest import HAS_HA

pytestmark = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")

if HAS_HA:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

    from custom_components.aerohive_local.const import DOMAIN
    from custom_components.aerohive_local.wifi import STORAGE_KEY

    from .test_init import MAC, USER_INPUT, FakeClient, entry, fake_client  # noqa: F401


def _cmds() -> list[str]:
    return [c for c in FakeClient.instances[-1].commands if not c.startswith("show")]


async def _ws(hass: HomeAssistant, hass_ws_client, msg: dict, token: str | None = None) -> dict:
    client = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
    await client.send_json({"id": 1, **msg})
    return await client.receive_json()


async def test_turn_network_off_for_a_while_and_back(hass: HomeAssistant, hass_ws_client, hass_storage, entry: MockConfigEntry) -> None:  # noqa: F811
    coordinator = entry.runtime_data
    radios = coordinator.data.radio_names()
    assert all(coordinator.data.ssid_on_radio("AP460C", r) for r in radios)
    FakeClient.instances[-1].commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "enabled": False, "duration_minutes": 30})
    assert reply["success"], reply
    assert [c for c in _cmds() if "ssid AP460C" in c] == [f"no interface {r} ssid AP460C" for r in radios]
    # Shown with the timer, and remembered across restarts.
    snap = (await _ws(hass, hass_ws_client, {"type": "aerohive_local/snapshot"}))["result"]
    assert [t["ssid"] for t in snap["wifi_timers"]] == ["AP460C"] and snap["wifi_timers"][0]["enabled"] is False
    stored = hass_storage[STORAGE_KEY]["data"]["timers"]
    assert stored[0]["restore"] == {entry.entry_id: radios}

    # When the time is up, exactly the radios it was on come back.
    with patch.object(coordinator, "async_update_ssid", wraps=coordinator.async_update_ssid) as update:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
        await hass.async_block_till_done()
    update.assert_called_once_with("AP460C", radios=radios)
    snap = (await _ws(hass, hass_ws_client, {"type": "aerohive_local/snapshot"}))["result"]
    assert snap["wifi_timers"] == [] and hass_storage[STORAGE_KEY]["data"]["timers"] == []


async def test_manual_change_cancels_timer_and_restore_now(hass: HomeAssistant, hass_ws_client, entry: MockConfigEntry) -> None:  # noqa: F811
    await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "enabled": False, "duration_minutes": 60})
    coordinator = entry.runtime_data
    with patch.object(coordinator, "async_update_ssid", wraps=coordinator.async_update_ssid) as update:
        reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/timer_cancel", "ssid": "AP460C", "restore": True})
    assert reply["success"]
    update.assert_called_once_with("AP460C", radios=coordinator.data.radio_names())
    snap = (await _ws(hass, hass_ws_client, {"type": "aerohive_local/snapshot"}))["result"]
    assert snap["wifi_timers"] == []
    # Turning a network on or off by hand replaces a running timer.
    await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "enabled": False, "duration_minutes": 60})
    await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "enabled": True})
    snap = (await _ws(hass, hass_ws_client, {"type": "aerohive_local/snapshot"}))["result"]
    assert snap["wifi_timers"] == []


async def test_create_edit_delete_network(hass: HomeAssistant, hass_ws_client, entry: MockConfigEntry) -> None:  # noqa: F811
    data = entry.runtime_data.data
    band_of = {r: ("2.4 GHz" if (data.interfaces[r].channel or 0) <= 14 else "5 GHz") for r in data.radio_names()}
    on_24 = [r for r, b in band_of.items() if b == "2.4 GHz"]
    FakeClient.instances[-1].commands.clear()
    reply = await _ws(hass, hass_ws_client, {
        "type": "aerohive_local/wifi/create", "ssid": "Kids", "password": "bedtime-2026", "bands": ["2.4 GHz"],
    })  # fmt: skip
    assert reply["success"], reply
    sent = _cmds()
    assert [c for c in sent if c.startswith("interface") and c.endswith("ssid Kids")] == [f"interface {r} ssid Kids" for r in on_24]
    assert not any("bedtime-2026" in c for c in FakeClient.instances[-1].commands if c.startswith("show"))

    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "password": "short"})
    assert not reply["success"] and "8" in reply["error"]["message"]
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "hidden": True, "max_clients": 20})
    assert reply["success"]
    assert entry.runtime_data.data.ssid_hidden("AP460C") is True

    FakeClient.instances[-1].commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/delete", "ssid": "AP460C"})
    assert reply["success"], reply
    assert any(c.startswith("no ") and "AP460C" in c for c in _cmds())
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/delete", "ssid": "Nope"})
    assert not reply["success"]


async def test_only_admins_change_wifi(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, entry: MockConfigEntry) -> None:  # noqa: F811
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/wifi/update", "ssid": "AP460C", "enabled": False}, hass_read_only_access_token)
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    # Reading is fine.
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/snapshot"}, hass_read_only_access_token)
    assert reply["success"]


async def test_timer_survives_restart(hass: HomeAssistant, hass_storage) -> None:
    """A timer that ran out while Home Assistant was down is applied once the AP is back."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"timers": [{
            "ssid": "AP460C", "until": (dt_util.utcnow() - timedelta(minutes=5)).isoformat(),
            "enabled": False, "restore": {"ap1": ["wifi0"]},
        }]},  # fmt: skip
    }
    ap = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=USER_INPUT, title="AH-7c0680", entry_id="ap1")
    ap.add_to_hass(hass)
    assert await hass.config_entries.async_setup(ap.entry_id)
    await hass.async_block_till_done()
    with patch.object(ap.runtime_data, "async_update_ssid", wraps=ap.runtime_data.async_update_ssid) as update:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
        await hass.async_block_till_done()
    update.assert_called_with("AP460C", radios=["wifi0"])
    assert hass_storage[STORAGE_KEY]["data"]["timers"] == []


async def test_set_ssid_action(hass: HomeAssistant, entry: MockConfigEntry) -> None:  # noqa: F811
    FakeClient.instances[-1].commands.clear()
    await hass.services.async_call(
        DOMAIN, "set_ssid", {"ssid": "AP460C", "enabled": False, "duration": {"hours": 2}}, blocking=True
    )
    assert any(c.startswith("no interface") and c.endswith("ssid AP460C") for c in _cmds())
    with pytest.raises(HomeAssistantError, match="duration needs"):
        await hass.services.async_call(DOMAIN, "set_ssid", {"ssid": "AP460C", "duration": {"hours": 1}}, blocking=True)
    with pytest.raises(HomeAssistantError, match="No reachable access point"):
        await hass.services.async_call(DOMAIN, "set_ssid", {"ssid": "Nope", "enabled": True}, blocking=True)
