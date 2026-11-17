"""WebSocket commands for the Network panel and dashboard cards.

- ``aerohive_local/snapshot``: one snapshot of every device.
- ``aerohive_local/subscribe``: a snapshot now, and again whenever a device
  updates (coalesced, at most four per second).

Changes are made through the integration's actions (``aerohive_local.set_port``
and others), so the same validation and safety checks apply as everywhere else.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from homeassistant.exceptions import HomeAssistantError

from . import wifi
from .const import DOMAIN
from .snapshot import network_snapshot

SIGNAL_SNAPSHOT = f"{DOMAIN}_snapshot_changed"
COALESCE_SECONDS = 0.25


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_snapshot)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_wifi_update)
    websocket_api.async_register_command(hass, ws_wifi_create)
    websocket_api.async_register_command(hass, ws_wifi_delete)
    websocket_api.async_register_command(hass, ws_wifi_timer_cancel)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/snapshot"})
@callback
def ws_snapshot(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], network_snapshot(hass))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    msg_id = msg["id"]
    pending: CALLBACK_TYPE | None = None

    @callback
    def send(_now: Any = None) -> None:
        nonlocal pending
        pending = None
        connection.send_message(websocket_api.event_message(msg_id, network_snapshot(hass)))

    @callback
    def changed() -> None:
        nonlocal pending
        if pending is None:
            pending = async_call_later(hass, COALESCE_SECONDS, send)

    disconnect = async_dispatcher_connect(hass, SIGNAL_SNAPSHOT, changed)

    @callback
    def unsubscribe() -> None:
        disconnect()
        if pending is not None:
            pending()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send()


# ----------------------------------------------------------------------
# Wi-Fi networks (admin only). WebSocket instead of actions so Wi-Fi
# passwords never travel over the event bus.
# ----------------------------------------------------------------------

_ENTRIES = vol.Optional("entry_ids")
_BANDS = vol.Optional("bands")


async def _run(connection: websocket_api.ActiveConnection, msg: dict[str, Any], coro: Any) -> None:
    try:
        await coro
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/wifi/update",
        vol.Required("ssid"): str,
        _ENTRIES: [str],
        vol.Optional("enabled"): bool,
        _BANDS: [str],
        vol.Optional("hidden"): bool,
        vol.Optional("max_clients"): vol.All(int, vol.Range(min=1, max=255)),
        vol.Optional("password"): str,
        vol.Optional("duration_minutes"): vol.All(int, vol.Range(min=1, max=7 * 24 * 60)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_wifi_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    minutes = msg.get("duration_minutes")
    await _run(
        connection,
        msg,
        wifi.async_update_network(
            hass,
            msg["ssid"],
            entry_ids=msg.get("entry_ids") or None,
            enabled=msg.get("enabled"),
            bands=msg.get("bands"),
            hidden=msg.get("hidden"),
            max_clients=msg.get("max_clients"),
            password=msg.get("password") or None,
            duration=timedelta(minutes=minutes) if minutes else None,
        ),
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/wifi/create",
        vol.Required("ssid"): str,
        vol.Optional("password"): str,
        _BANDS: [str],
        vol.Optional("hidden", default=False): bool,
        _ENTRIES: [str],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_wifi_create(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(
        connection,
        msg,
        wifi.async_create_network(
            hass,
            msg["ssid"].strip(),
            password=msg.get("password") or None,
            bands=msg.get("bands"),
            hidden=msg["hidden"],
            entry_ids=msg.get("entry_ids") or None,
        ),
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/wifi/delete", vol.Required("ssid"): str, _ENTRIES: [str]})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_wifi_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, wifi.async_delete_network(hass, msg["ssid"], entry_ids=msg.get("entry_ids") or None))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/wifi/timer_cancel", vol.Required("ssid"): str, vol.Optional("restore", default=True): bool}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_wifi_timer_cancel(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, wifi.get_timers(hass).async_cancel(msg["ssid"], revert=msg["restore"]))
