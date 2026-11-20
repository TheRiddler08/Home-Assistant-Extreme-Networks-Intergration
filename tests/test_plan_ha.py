"""Network setup in Home Assistant: saving the plan, encrypted secrets, preview, apply with rollback, keys."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from .conftest import HAS_HA

pytestmark = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")

if HAS_HA:
    from homeassistant.core import HomeAssistant
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

    from custom_components.aerohive_local.plan import STORAGE_KEY, get_plan

    from .test_init import FakeClient, entry, fake_client  # noqa: F401
    from .test_openwrt_ha import ow_entry  # noqa: F401

CONFIRM = "rm -f /tmp/hn_backup/pending && echo HN_CONFIRMED"
STAGED = "for c in network dhcp firewall wireless; do uci -q changes $c; done"


@pytest.fixture(autouse=True)
def fast_confirm():
    with (
        patch("custom_components.aerohive_local.coordinator.CONFIRM_DELAY_SECONDS", 0),
        patch("custom_components.aerohive_local.coordinator.CONFIRM_RETRY_SECONDS", 0),
        patch("custom_components.aerohive_local.coordinator.ROLLBACK_SECONDS", 0),
    ):
        yield


def _plan(router: str | None) -> dict:
    return {
        "networks": [
            {"id": "home", "name": "Home", "vlan": 50, "subnet": "10.40.33.1/24", "reach": ["iot"]},
            {"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24"},
        ],
        "wifi": [
            {"id": "home", "ssid": "home", "network": "home", "security": "ppsk", "bands": ["2.4 GHz", "5 GHz"]},
            {"id": "guest", "ssid": "Guest Wifi", "network": "iot", "security": "wpa2", "bands": ["2.4 GHz"]},
        ],
        "keys": [],
        "devices": {"router": router, "router_trunks": ["br-lan"], "switch_trunks": {}, "aps": None},
    }


async def _ws(hass: HomeAssistant, hass_ws_client, msg: dict, token: str | None = None) -> dict:
    client = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
    await client.send_json({"id": 1, **msg})
    return await client.receive_json()


def _client() -> FakeClient:
    return FakeClient.instances[-1]


async def test_save_preview_apply_on_openwrt(hass: HomeAssistant, hass_ws_client, hass_storage, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/save", "plan": _plan(ow_entry.entry_id), "secrets": {"wifi:guest": "guest-pass-1"}})
    assert reply["success"], reply
    state = reply["result"]
    assert state["errors"] == [] and state["pending"] is True
    assert "wifi:guest" in state["secrets"] and "wifi:home" in state["secrets"]  # per-device key Wi-Fi gets a base key
    assert next(d for d in state["devices"] if d["entry_id"] == ow_entry.entry_id)["role"] == "router"
    # Secrets are stored encrypted.
    stored = str(hass_storage[STORAGE_KEY])
    assert "guest-pass-1" not in stored

    preview = (await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/preview"}))["result"]
    router = preview["devices"][0]
    assert router["role"] == "router" and router["errors"] == []
    assert any("firewall.hn_fwd50_30.dest=hn30" in c for c in router["commands"])
    assert "guest-pass-1" not in str(preview) and any(".key=***" in c for c in router["commands"])

    _client().responses[CONFIRM] = "HN_CONFIRMED"
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/apply"})
    assert reply["success"], reply
    assert reply["result"]["results"][0]["ok"] is True
    assert reply["result"]["pending"] is False and reply["result"]["applied_at"]
    sent = _client().commands
    assert any(c.startswith("( mkdir -p /tmp/hn_backup") for c in sent)
    assert any("uci set network.hn_if50.ipaddr=10.40.33.1" in c for c in sent)
    assert any("sh /tmp/hn_backup/rollback.sh" in c and "uci commit wireless" in c for c in sent)
    assert CONFIRM in sent

    # The key is only shown on request, to admins.
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/reveal", "ref": "wifi:guest"})
    assert reply["result"]["value"] == "guest-pass-1" and reply["result"]["ssid"] == "Guest Wifi"
    assert reply["result"]["qr"].startswith("<svg")


async def test_apply_refused_or_rolled_back(hass: HomeAssistant, hass_ws_client, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    manager = get_plan(hass)
    await manager.async_save_plan(_plan(ow_entry.entry_id), {"wifi:guest": "guest-pass-1"})
    # Unsaved LuCI changes: nothing is touched.
    _client().responses[STAGED] = "network.lan.ipaddr='10.0.0.1'"
    results = await manager.async_apply()
    assert "aren't saved yet" in results[0]["error"]
    assert not any("uci set" in c for c in _client().commands)
    del _client().responses[STAGED]

    # A rejected command reverts everything staged.
    original = FakeClient.run

    async def reject(self, command, **kwargs):
        out = await original(self, command, **kwargs)
        if "hn_zone" in command:
            return "uci: Invalid argument\nERROR: OpenWrt did not accept the change"
        return out

    with patch.object(FakeClient, "run", reject):
        results = await manager.async_apply()
    assert "Nothing was changed" in results[0]["error"] and "guest-pass-1" not in results[0]["error"]
    assert "for c in network dhcp firewall wireless; do uci revert $c; done" in _client().commands
    assert manager.applied is None

    # Applied, but Home Assistant can't confirm: the router puts the old settings back.
    results = await manager.async_apply()
    assert "goes back to the previous settings" in results[0]["error"]


async def test_keys_added_revoked_and_expire(hass: HomeAssistant, hass_ws_client, freezer, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    manager = get_plan(hass)
    await manager.async_save_plan(_plan(ow_entry.entry_id), {"wifi:guest": "guest-pass-1"})
    _client().responses[CONFIRM] = "HN_CONFIRMED"
    assert all(r["ok"] for r in await manager.async_apply())

    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_add", "name": "Living room TV", "wifi": "home", "network": "iot"})
    assert reply["success"], reply
    kid = reply["result"]["key_id"]
    assert kid == "living_roo" and reply["result"]["results"][0]["ok"]
    key = manager.secret(f"key:{kid}")
    assert key and len(key) == 12
    assert any(f"wireless.hn_k_{kid}_radio0.vid=30" in c for c in _client().commands)

    _client().commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_add", "name": "Google setup", "wifi": "home", "minutes": 15})
    temp = reply["result"]["key_id"]
    assert any(f"hn_k_{temp}_" in c for c in _client().commands)
    # A key change only touches the Wi-Fi configuration and reloads only Wi-Fi.
    assert not any("uci set network." in c or "uci commit network" in c for c in _client().commands)
    assert "(trap '' HUP INT TERM; sleep 1; wifi reload) </dev/null >/dev/null 2>&1 &" in _client().commands

    _client().commands.clear()
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [k["id"] for k in manager.plan["keys"]] == [kid]
    assert manager.secret(f"key:{temp}") is None
    pushed = " ".join(_client().commands)
    assert f"hn_k_{kid}_" in pushed and f"hn_k_{temp}_" not in pushed
    assert manager.audit[-1]["action"] == "key_expired"

    # Saving the layout from a page that doesn't know the keys keeps them.
    await manager.async_save_plan({**manager.plan, "keys": []}, {})
    assert [k["id"] for k in manager.plan["keys"]] == [kid] and manager.secret(f"key:{kid}")

    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_revoke", "key_id": kid})
    assert reply["success"], reply
    assert reply["result"]["plan"]["keys"] == []

    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_add", "name": "x", "wifi": "guest"})
    assert not reply["success"] and "per-device keys" in reply["error"]["message"]


async def test_aerohive_ap_gets_its_part(hass: HomeAssistant, entry: MockConfigEntry) -> None:  # noqa: F811
    manager = get_plan(hass)
    errors = await manager.async_save_plan(_plan(None), {"wifi:guest": "guest-pass-1"})
    assert errors == []
    preview = manager.preview()
    assert preview["errors"] == [] and "No OpenWrt router chosen" in preview["notes"][0]
    ap = preview["devices"][0]
    assert ap["role"] == "ap"
    assert "user-profile hn30 qos-policy def-user-qos vlan-id 30 attribute 30" in ap["commands"]
    assert any("per-device keys" in w for w in ap["warnings"])
    results = await manager.async_apply()
    assert results[0]["ok"], results
    sent = _client().commands
    assert "security-object \"Guest Wifi\" default-user-profile-attr 30" in sent
    assert manager.managed[entry.entry_id]["ssids"] == ["Guest Wifi"]
    # Removing it from the plan removes it from the AP.
    plan = _plan(None)
    plan["wifi"] = plan["wifi"][:1]
    await manager.async_save_plan(plan, {})
    assert 'no ssid "Guest Wifi"' in manager.preview()["devices"][0]["commands"]


async def test_plan_is_admin_only_and_survives_restart(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, hass_storage, entry: MockConfigEntry) -> None:  # noqa: F811
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/get"}, hass_read_only_access_token)
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/template"})
    assert len(reply["result"]["plan"]["networks"]) == 10
    manager = get_plan(hass)
    await manager.async_save_plan(_plan(None), {"wifi:guest": "guest-pass-1"})
    # A second manager (as after a restart) reads it back and decrypts the secret.
    from custom_components.aerohive_local.plan import PlanManager

    again = PlanManager(hass)
    await again.async_load()
    again.async_unload()
    assert again.plan["networks"][0]["id"] == "home"
    assert again.secret("wifi:guest") == "guest-pass-1"
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/save", "plan": _plan(None), "secrets": {"wifi:guest": "short"}})
    assert not reply["success"] and "8-63" in reply["error"]["message"]


def _portal_plan(router: str) -> dict:
    plan = _plan(router)
    plan["networks"].append({"id": "onboard", "name": "homeOB", "vlan": 80, "subnet": "10.8.10.1/24", "internet": False})
    plan["wifi"].append({"id": "homeob", "ssid": "homeOB", "network": "onboard", "security": "wpa2", "bands": ["2.4 GHz"], "max_clients": 5, "isolate": True})
    plan["onboarding"] = {"enabled": True, "wifi": "homeob", "offer": ["home"], "title": "Welcome home", "text": "Hi"}
    return plan


async def test_onboarding_portal(hass: HomeAssistant, hass_ws_client, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    from custom_components.aerohive_local import portal

    mark = "ERROR: OpenWrt did not accept the change"
    manager = get_plan(hass)
    await manager.async_save_plan(_portal_plan(ow_entry.entry_id), {"wifi:guest": "guest-pass-1", "wifi:homeob": "homeob-pass-1"})
    assert manager.state()["errors"] == []
    client = _client()
    client.responses[CONFIRM] = "HN_CONFIRMED"
    client.responses[f"{portal.UHTTPD_CHECK} || echo '{mark}'"] = "HN_UHTTPD_OK"
    results = await manager.async_apply()
    assert results[0]["ok"], results
    sent = "\n".join(client.commands)
    assert "uci set firewall.hn_portal80_dnat.target=DNAT" in sent
    assert "uci add_list uhttpd.hn_portal.listen_http=10.8.10.1:8099" in sent and "/www/hn-portal/cgi-bin/portal.new" in sent
    assert manager.state()["portal"] == {"on": True, "url": "http://10.8.10.1:8099/", "error": None}

    # A device asks on the portal; Home Assistant picks it up, notifies and fires an event.
    events = []
    hass.bus.async_listen("aerohive_local_onboarding_request", lambda e: events.append(e.data))
    listing = f"{portal.LIST_REQUESTS} || echo '{mark}'"
    client.responses[listing] = (
        "== 021122334455\nmac=02:11:22:33:44:55\nip=10.8.10.60\nname=Living room TV\nkind=tv\nwants=home\ntok=abcdefghij012345\ntime=1\n"
        "== 021122334466\nmac=02:11:22:33:44:66\nip=10.8.10.61\nname=Stranger\nkind=other\nwants=\ntok=bbbbbbbbbbbbbbbb\ntime=1\n"
    )
    await manager._async_portal_tick()
    await hass.async_block_till_done()
    state = manager.state()
    assert {r["name"] for r in state["requests"]} == {"Living room TV", "Stranger"}
    assert all("token" not in r for r in state["requests"])
    assert {e["name"] for e in events} == {"Living room TV", "Stranger"}
    await manager._async_portal_tick()  # seen again: no second notification
    assert len(events) == 2

    # Approve: a key is made and pushed, and the portal page with key and QR is written to the router.
    client.commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/onboard_approve", "request_id": "021122334455", "wifi": "home", "network": "iot", "minutes": 60})
    assert reply["success"], reply
    key = manager.secret(f"key:{reply['result']['key_id']}")
    sent = "\n".join(client.commands)
    assert f"wireless.hn_k_{reply['result']['key_id']}_radio0.vid=30" in sent
    assert "/tmp/hn_portal/ok/021122334455.html.new" in sent and key in sent and "&lt;svg" not in sent and "svg" in sent
    assert "printf '%s' abcdefghij012345 >> /tmp/hn_portal/ok/021122334455.tok.new" in sent
    assert next(r for r in reply["result"]["requests"] if r["id"] == "021122334455")["status"] == "approved"

    # Decline the other one.
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/onboard_decline", "request_id": "021122334466"})
    assert reply["success"]
    assert "mkdir -p /tmp/hn_portal/no && : > /tmp/hn_portal/no/021122334466 && rm -f /tmp/hn_portal/req/021122334466 || echo" in "\n".join(client.commands)
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/onboard_decline", "request_id": "021122334466"})
    assert not reply["success"] and "no longer waiting" in reply["error"]["message"]

    # Reveal gives a QR code for a device key.
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/reveal", "ref": f"key:{manager.plan['keys'][0]['id']}"})
    assert reply["result"]["ssid"] == "home" and reply["result"]["qr"].startswith("<svg")
    assert [a["action"] for a in manager.audit][-2:] == ["onboarding_approved", "onboarding_declined"]


async def test_portal_preview_and_template(hass: HomeAssistant, hass_ws_client, entry: MockConfigEntry) -> None:  # noqa: F811
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/portal_template"})
    assert "{{content}}" in reply["result"]["template"] and "{{logo}}" in reply["result"]["placeholders"]
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/portal_preview", "page": "approved",
                                             "onboarding": {"title": "Acme", "colors": {"accent": "#ff6600"}}})
    page = reply["result"]["html"]
    assert "<h1>Acme</h1>" in page and "--accent:#ff6600" in page and "<svg" in page and "Kx7mPq2vR9tw" in page


async def test_key_rotation(hass: HomeAssistant, hass_ws_client, freezer, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    manager = get_plan(hass)
    await manager.async_save_plan(_plan(ow_entry.entry_id), {"wifi:guest": "guest-pass-1"})
    _client().responses[CONFIRM] = "HN_CONFIRMED"
    assert all(r["ok"] for r in await manager.async_apply())
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_add", "name": "Nest speaker", "wifi": "home", "rotate_days": 182})
    kid = reply["result"]["key_id"]
    old = manager.secret(f"key:{kid}")
    events = []
    hass.bus.async_listen("aerohive_local_key_rotation", lambda e: events.append(e.data))

    # Manual rotation: old and new key both work until confirmed.
    _client().commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_rotate", "key_ids": [kid], "cutoff_days": 14})
    assert reply["success"], reply
    new = manager.secret(f"key:{kid}:next")
    assert new and new != old
    sent = " ".join(_client().commands)
    assert f"wireless.hn_k_{kid}_radio0.key={old}" in sent and f"wireless.hn_k_{kid}n_radio0.key={new}" in sent
    await hass.async_block_till_done()  # events are delivered on the next loop turn
    item = reply["result"]["plan"]["keys"][0]
    assert item["rotation"]["cutoff"] and events and events[0]["key_id"] == kid
    reveal = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/reveal", "ref": f"key:{kid}:next"})
    assert reveal["result"]["value"] == new and reveal["result"]["qr"].startswith("<svg")
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_rotate", "key_ids": [kid]})
    assert not reply["success"] and "already" in reply["error"]["message"]

    # Device updated: the new key becomes the key, the old one stops.
    _client().commands.clear()
    reply = await _ws(hass, hass_ws_client, {"type": "aerohive_local/plan/key_rotate_finish", "key_id": kid})
    assert reply["success"] and manager.secret(f"key:{kid}") == new and manager.secret(f"key:{kid}:next") is None
    sent = " ".join(_client().commands)
    assert old not in sent and f"hn_k_{kid}n_" not in sent and reply["result"]["plan"]["keys"][0]["rotated"]

    # Scheduled: after 182 days a rotation starts by itself; with a cutoff it finishes by itself.
    manager.plan["keys"][0]["cutoff_days"] = 7
    freezer.tick(timedelta(days=183))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert manager.plan["keys"][0]["rotation"] and manager.secret(f"key:{kid}:next")
    upcoming = manager.secret(f"key:{kid}:next")
    freezer.tick(timedelta(days=8))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert manager.plan["keys"][0]["rotation"] is None and manager.secret(f"key:{kid}") == upcoming
    assert [a["action"] for a in manager.audit if a["action"].startswith("key_rot")][-2:] == ["key_rotation_started", "key_rotated"]

    # Cancel keeps the old key.
    await manager.async_rotate_keys([kid])
    current = manager.secret(f"key:{kid}")
    await manager.async_cancel_rotation(kid)  # (the test's login token has expired after the time jumps)
    assert manager.secret(f"key:{kid}") == current and manager.secret(f"key:{kid}:next") is None


async def test_onboarding_rules_and_actions(hass: HomeAssistant, hass_read_only_user, ow_entry: MockConfigEntry) -> None:  # noqa: F811
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized
    from custom_components.aerohive_local import portal

    mark = "ERROR: OpenWrt did not accept the change"
    manager = get_plan(hass)
    plan = _portal_plan(ow_entry.entry_id)
    plan["onboarding"]["kinds"] = ["Phone", "TV", "Other"]
    plan["onboarding"]["rules"] = [
        {"kind": "tv", "wifi": "home", "network": "iot", "minutes": None, "auto": True},
        {"kind": "phone", "wifi": "home", "network": None, "minutes": 60, "auto": False},
    ]
    await manager.async_save_plan(plan, {"wifi:guest": "guest-pass-1", "wifi:homeob": "homeob-pass-1"})
    assert manager.state()["errors"] == []
    client = _client()
    client.responses[CONFIRM] = "HN_CONFIRMED"
    client.responses[f"{portal.UHTTPD_CHECK} || echo '{mark}'"] = "HN_UHTTPD_OK"
    assert all(r["ok"] for r in await manager.async_apply())
    events = []
    hass.bus.async_listen("aerohive_local_onboarding_request", lambda e: events.append(e.data))
    client.responses[f"{portal.LIST_REQUESTS} || echo '{mark}'"] = (
        "== 021122334455\nmac=02:11:22:33:44:55\nname=Living room TV\nkind=tv\nwants=\ntok=abcdefghij012345\ntime=1\n"
        "== 021122334466\nmac=02:11:22:33:44:66\nname=Pixel\nkind=phone\nwants=\ntok=bbbbbbbbbbbbbbbb\ntime=1\n"
    )
    await manager._async_portal_tick()
    await hass.async_block_till_done()
    tv, phone = manager.requests["021122334455"], manager.requests["021122334466"]
    assert tv["status"] == "approved" and phone["status"] == "pending"  # TVs are approved by their rule
    assert phone["suggested"] == {"wifi": "home", "network": None, "minutes": 60, "auto": False}
    tv_key = next(k for k in manager.plan["keys"] if k["name"] == "Living room TV")
    assert tv_key["network"] == "iot"
    assert {(e["name"], e["auto_approved"], e["kind_label"]) for e in events} == {("Living room TV", True, "TV"), ("Pixel", False, "Phone")}

    # Approve from an automation (no user) with the rule's defaults; non-admin users are refused.
    with pytest.raises(Unauthorized):
        await hass.services.async_call("aerohive_local", "approve_device", {"request_id": "021122334466"}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    await hass.services.async_call("aerohive_local", "approve_device", {"request_id": "021122334466"}, blocking=True)
    phone_key = next(k for k in manager.plan["keys"] if k["name"] == "Pixel")
    assert phone["status"] == "approved" and phone_key["expires"]  # the rule's 60 minutes
    from homeassistant.exceptions import ServiceValidationError
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("aerohive_local", "approve_device", {"request_id": "nope"}, blocking=True)
