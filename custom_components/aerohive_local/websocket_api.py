"""WebSocket commands for the Network panel and dashboard cards.

- ``aerohive_local/snapshot``: one snapshot of every device.
- ``aerohive_local/subscribe``: a snapshot now, and again whenever a device
  updates (coalesced, at most one per second).

Changes are made through the integration's actions (``aerohive_local.set_port``
and others), so the same validation and safety checks apply as everywhere else.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .snapshot import network_snapshot

SIGNAL_SNAPSHOT = f"{DOMAIN}_snapshot_changed"
COALESCE_SECONDS = 1.0


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_snapshot)
    websocket_api.async_register_command(hass, ws_subscribe)


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
