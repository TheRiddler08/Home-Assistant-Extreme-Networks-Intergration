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

from . import plan as plan_mod
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
    for command in (ws_plan_get, ws_plan_save, ws_plan_preview, ws_plan_apply, ws_plan_reveal, ws_plan_template, ws_key_add, ws_key_revoke, ws_onboard_approve, ws_onboard_decline, ws_portal_preview, ws_portal_template, ws_key_rotate, ws_key_rotate_finish, ws_key_rotate_cancel, ws_client_rename, ws_fixed_ip, ws_fixed_ip_remove, ws_block, ws_unblock, ws_forwards):
        websocket_api.async_register_command(hass, command)


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
    disconnect_plan = async_dispatcher_connect(hass, plan_mod.SIGNAL_PLAN, changed)  # fixed IPs, blocks

    @callback
    def unsubscribe() -> None:
        disconnect()
        disconnect_plan()
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


# ----------------------------------------------------------------------
# Network setup (admin only): the plan, preview, apply, keys.
# ----------------------------------------------------------------------


def _user(connection: websocket_api.ActiveConnection) -> str | None:
    user = getattr(connection, "user", None)
    return getattr(user, "name", None)


async def _plan_call(connection: websocket_api.ActiveConnection, msg: dict[str, Any], coro: Any) -> None:
    try:
        result = await coro
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/get"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], plan_mod.get_plan(hass).state())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/save",
        vol.Required("plan"): dict,
        vol.Optional("secrets", default={}): {str: vol.Any(str, None)},
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        await manager.async_save_plan(msg["plan"], msg["secrets"], _user(connection))
        return manager.state()

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/preview"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], plan_mod.get_plan(hass).preview())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/apply", vol.Optional("entry_ids"): [str]})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        results = await manager.async_apply(msg.get("entry_ids") or None, _user(connection))
        return {"results": results, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/reveal", vol.Required("ref"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_reveal(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    result = plan_mod.get_plan(hass).reveal(msg["ref"])
    if result is None:
        connection.send_error(msg["id"], "not_found", "No password or key stored for this")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/template"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_plan_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    from . import netplan  # noqa: PLC0415

    connection.send_result(msg["id"], {"plan": netplan.template()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/key_add",
        vol.Required("name"): str,
        vol.Required("wifi"): str,
        vol.Optional("network"): vol.Any(str, None),
        vol.Optional("mac"): vol.Any(str, None),
        vol.Optional("minutes"): vol.Any(None, vol.All(int, vol.Range(min=1, max=366 * 24 * 60))),
        vol.Optional("key"): vol.Any(str, None),
        vol.Optional("rotate_days"): vol.Any(None, vol.All(int, vol.Range(min=1, max=3650))),
        vol.Optional("cutoff_days"): vol.Any(None, vol.All(int, vol.Range(min=1, max=365))),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_key_add(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        added = await manager.async_add_key(
            name=msg["name"],
            wifi=msg["wifi"],
            network=msg.get("network"),
            mac=msg.get("mac"),
            minutes=msg.get("minutes"),
            key=msg.get("key") or None,
            rotate_days=msg.get("rotate_days"),
            cutoff_days=msg.get("cutoff_days"),
            user=_user(connection),
        )
        return {"key_id": added["id"], "results": added["results"], **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/key_revoke", vol.Required("key_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_key_revoke(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        results = await manager.async_revoke_key(msg["key_id"], _user(connection))
        return {"results": results, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/onboard_approve",
        vol.Required("request_id"): str,
        vol.Required("wifi"): str,
        vol.Optional("network"): vol.Any(str, None),
        vol.Optional("minutes"): vol.Any(None, vol.All(int, vol.Range(min=1, max=366 * 24 * 60))),
        vol.Optional("bind_mac", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_onboard_approve(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        approved = await manager.async_approve(
            msg["request_id"],
            wifi=msg["wifi"],
            network=msg.get("network") or None,
            minutes=msg.get("minutes"),
            bind_mac=msg["bind_mac"],
            user=_user(connection),
        )
        return {**approved, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/onboard_decline", vol.Required("request_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_onboard_decline(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        await manager.async_decline(msg["request_id"], _user(connection))
        return manager.state()

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/portal_preview",
        vol.Optional("onboarding", default={}): dict,
        vol.Optional("page", default="form"): vol.In(["form", "waiting", "approved", "declined"]),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_portal_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    html = plan_mod.get_plan(hass).portal_preview(msg["onboarding"], msg["page"])
    connection.send_result(msg["id"], {"html": html})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/portal_template"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_portal_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    from . import portal  # noqa: PLC0415

    connection.send_result(msg["id"], {"template": portal.DEFAULT_TEMPLATE, "placeholders": list(portal.PLACEHOLDERS)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/key_rotate",
        vol.Required("key_ids"): [str],
        vol.Optional("cutoff_days"): vol.Any(None, vol.All(int, vol.Range(min=1, max=365))),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_key_rotate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        results = await manager.async_rotate_keys(msg["key_ids"], cutoff_days=msg.get("cutoff_days"), user=_user(connection))
        return {"results": results, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/key_rotate_finish", vol.Required("key_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_key_rotate_finish(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        results = await manager.async_finish_rotation(msg["key_id"], user=_user(connection))
        return {"results": results, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/key_rotate_cancel", vol.Required("key_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_key_rotate_cancel(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = plan_mod.get_plan(hass)

    async def run() -> dict[str, Any]:
        results = await manager.async_cancel_rotation(msg["key_id"], user=_user(connection))
        return {"results": results, **manager.state()}

    await _plan_call(connection, msg, run())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/client/rename", vol.Required("mac"): str, vol.Optional("name"): vol.Any(str, None)}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_client_rename(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Give a connected device a nickname (empty: remove it)."""
    from . import clients  # noqa: PLC0415
    from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415

    try:
        await clients.async_set_nickname(hass, msg["mac"], msg.get("name"))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    async_dispatcher_send(hass, SIGNAL_SNAPSHOT)
    connection.send_result(msg["id"])


def _extras_call(name: str):
    """A router-extras command: run it, answer with what was done and the plan state."""

    async def handler(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        manager = plan_mod.get_plan(hass)
        user = _user(connection)
        method = getattr(manager, name)
        args = {k: v for k, v in msg.items() if k not in ("id", "type")}

        async def run() -> dict[str, Any]:
            done = await method(**args, user=user)
            return {"done": done, **manager.state()}

        await _plan_call(connection, msg, run())

    return handler


_MINUTES = vol.Any(None, vol.All(int, vol.Range(min=1, max=30 * 24 * 60)))

ws_fixed_ip = websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/plan/fixed_ip", vol.Required("mac"): str, vol.Required("ip"): str, vol.Optional("name", default=""): str}
)(websocket_api.require_admin(websocket_api.async_response(_extras_call("async_set_reservation"))))
ws_fixed_ip_remove = websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/fixed_ip_remove", vol.Required("mac"): str})(
    websocket_api.require_admin(websocket_api.async_response(_extras_call("async_remove_reservation")))
)
ws_block = websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/plan/block", vol.Required("mac"): str, vol.Optional("minutes"): _MINUTES, vol.Optional("name", default=""): str}
)(websocket_api.require_admin(websocket_api.async_response(_extras_call("async_block"))))
ws_unblock = websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/plan/unblock", vol.Required("mac"): str})(
    websocket_api.require_admin(websocket_api.async_response(_extras_call("async_unblock")))
)
ws_forwards = websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/forwards",
        vol.Required("forwards"): [
            {
                vol.Required("name"): str,
                vol.Optional("id"): vol.Any(str, None),
                vol.Required("protocol"): vol.In(["tcp", "udp", "both"]),
                vol.Required("port"): vol.Coerce(str),
                vol.Required("to_ip"): str,
                vol.Optional("to_port"): vol.Any(None, vol.Coerce(str)),
                vol.Optional("enabled", default=True): bool,
            }
        ],
    }
)(websocket_api.require_admin(websocket_api.async_response(_extras_call("async_save_forwards"))))
