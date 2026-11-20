"""The network plan in Home Assistant: storage, encrypted secrets, apply, expiring keys.

- The plan (networks, Wi-Fi, access rules, keys, device roles) is stored in
  ``.storage/aerohive_local.network_plan``.
- Wi-Fi passwords and per-device keys are stored encrypted (Fernet) in the
  same file, with the encryption key in a separate file
  (``.storage/aerohive_local.vault_key``, readable only by Home Assistant).
  They never appear in snapshots, entity states, events or logs.
- Applying sends each device its part: switches first (so VLANs exist on the
  trunks), then the router, then access points. OpenWrt devices roll back on
  their own if Home Assistant loses contact after the change.
- Keys that expire are removed and the affected devices updated, also after
  a restart (checked every minute).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import secrets as pysecrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.components import persistent_notification
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import commands
from . import netplan
from . import portal
from . import qr
from .const import DOMAIN
from .parsers import radio_band

if TYPE_CHECKING:
    from .coordinator import AerohiveCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.network_plan"
STORAGE_VERSION = 1
KEY_FILE = f"{DOMAIN}.vault_key"
DATA_PLAN = f"{DOMAIN}_network_plan"
SIGNAL_PLAN = f"{DOMAIN}_plan_changed"
EXPIRY_INTERVAL = timedelta(minutes=1)
PORTAL_INTERVAL = timedelta(seconds=15)
REQUEST_TTL = timedelta(hours=1)  # unanswered requests, and approved pages, are removed after this
EVENT_ONBOARDING = f"{DOMAIN}_onboarding_request"
EVENT_ROTATION = f"{DOMAIN}_key_rotation"
AUDIT_LIMIT = 200
KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

ROLE_ROUTER = "router"
ROLE_AP = "ap"
ROLE_SWITCH = "switch"
ROLE_NONE = "none"
_ORDER = {ROLE_SWITCH: 0, ROLE_ROUTER: 1, ROLE_AP: 2}


def generate_key(length: int = 12) -> str:
    return "".join(pysecrets.choice(KEY_ALPHABET) for _ in range(length))


def _coordinators(hass: HomeAssistant) -> list[AerohiveCoordinator]:
    from .coordinator import AerohiveCoordinator  # noqa: PLC0415 - avoid an import cycle

    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and isinstance(getattr(entry, "runtime_data", None), AerohiveCoordinator)
    ]


def radio_bands(coordinator: AerohiveCoordinator) -> dict[str, str | None]:
    data = coordinator.data
    result: dict[str, str | None] = {}
    for radio in data.radio_names():
        iface = data.interfaces.get(radio)
        result[radio] = radio_band(iface.radio_profile if iface else None, iface.channel if iface else None)
    return result


class PlanManager:
    """Holds the plan and applies it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self.plan: dict[str, Any] = netplan.empty_plan()
        self.applied: dict[str, Any] | None = None  # the plan as last applied
        self.applied_at: str | None = None
        self._secrets: dict[str, str] = {}  # ref -> Fernet token
        self.managed: dict[str, dict[str, list[str]]] = {}  # entry_id -> {"ssids"/"vlans": [...]}
        self.audit: list[dict[str, Any]] = []
        self.last_results: list[dict[str, Any]] = []
        self.needs_apply: set[str] = set()  # devices whose key update failed (retried every minute)
        self._fernet: Fernet | None = None
        self._lock = asyncio.Lock()
        self._unsub = None
        self._unsub_portal = None
        self.requests: dict[str, dict[str, Any]] = {}  # onboarding requests seen on the router
        self.portal_error: str | None = None

    # ------------------------------------------------------------------ storage

    def _load_key(self) -> bytes:
        path = self.hass.config.path(".storage", KEY_FILE)
        if os.path.exists(path):
            with open(path, "rb") as file:
                return file.read().strip()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(key)
        return key

    async def async_load(self) -> None:
        self._fernet = Fernet(await self.hass.async_add_executor_job(self._load_key))
        stored = await self._store.async_load() or {}
        self.plan = netplan.normalize(stored.get("plan"))
        self.applied = netplan.normalize(stored["applied"]) if stored.get("applied") else None
        self.applied_at = stored.get("applied_at")
        self._secrets = dict(stored.get("secrets") or {})
        self.managed = dict(stored.get("managed") or {})
        self.audit = list(stored.get("audit") or [])[-AUDIT_LIMIT:]
        self.needs_apply = set(stored.get("needs_apply") or [])
        self.requests = dict(stored.get("requests") or {})
        self._unsub = async_track_time_interval(self.hass, self._async_tick, EXPIRY_INTERVAL)
        self._unsub_portal = async_track_time_interval(self.hass, self._async_portal_tick, PORTAL_INTERVAL)

    @callback
    def async_unload(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_portal:
            self._unsub_portal()
            self._unsub_portal = None

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "plan": self.plan,
                "applied": self.applied,
                "applied_at": self.applied_at,
                "secrets": self._secrets,
                "managed": self.managed,
                "audit": self.audit[-AUDIT_LIMIT:],
                "needs_apply": sorted(self.needs_apply),
                "requests": self.requests,
            }
        )
        async_dispatcher_send(self.hass, SIGNAL_PLAN)

    def _log(self, action: str, detail: str, user: str | None = None) -> None:
        self.audit.append({"time": dt_util.utcnow().isoformat(), "action": action, "detail": detail, "user": user})
        self.audit = self.audit[-AUDIT_LIMIT:]

    # ------------------------------------------------------------------ secrets

    def secret(self, ref: str) -> str | None:
        token = self._secrets.get(ref)
        if not token or self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            _LOGGER.warning("A stored network secret (%s) can't be decrypted; set it again", ref)
            return None

    def _set_secret(self, ref: str, value: str | None) -> None:
        if not value:
            self._secrets.pop(ref, None)
            return
        assert self._fernet is not None
        self._secrets[ref] = self._fernet.encrypt(value.encode()).decode()

    def reveal(self, ref: str) -> dict[str, Any] | None:
        """A stored password or key, with a QR code to join its Wi-Fi network."""
        value = self.secret(ref)
        if value is None:
            return None
        kind, _, item_id = ref.partition(":")
        item_id = item_id.split(":")[0]  # "key:<id>:next" is the upcoming key of a rotation
        wifi = None
        if kind == "key":
            key = next((k for k in self.plan["keys"] if k["id"] == item_id), None)
            wifi = next((w for w in self.plan["wifi"] if key and w["id"] == key["wifi"]), None)
        elif kind == "wifi":
            wifi = next((w for w in self.plan["wifi"] if w["id"] == item_id), None)
            if wifi is not None and wifi["security"] == "ppsk":
                return {"value": value, "ssid": wifi["ssid"], "qr": None}  # the base key isn't for joining
        if wifi is None:
            return {"value": value, "ssid": None, "qr": None}
        payload = qr.wifi_payload(wifi["ssid"], value, wifi["security"], bool(wifi.get("hidden")))
        return {"value": value, "ssid": wifi["ssid"], "qr": qr.svg(payload)}

    def secrets_set(self) -> list[str]:
        return sorted(ref for ref in self._secrets if self.secret(ref))

    # ------------------------------------------------------------------ editing

    async def async_save_plan(self, plan: dict[str, Any], secrets: dict[str, str | None], user: str | None = None) -> list[str]:
        """Store a new plan (not applied yet). Returns its problems (it's stored anyway).

        Keys aren't taken from ``plan``: they're added and revoked on their own
        (async_add_key / async_revoke_key), so a stale page can't drop them.
        """
        plan = netplan.normalize({**plan, "keys": self.plan["keys"]})
        for ref, value in secrets.items():
            if value:
                try:
                    commands.validate_psk(value)
                except commands.InvalidArgument as err:
                    raise HomeAssistantError(f"{ref.split(':', 1)[-1]}: {err}") from err
        async with self._lock:
            for ref, value in secrets.items():
                self._set_secret(ref, value)
            # A per-device key Wi-Fi needs a base key on OpenWrt; nobody is given it.
            for wifi in plan["wifi"]:
                if wifi["security"] == "ppsk" and not self.secret(f"wifi:{wifi['id']}"):
                    self._set_secret(f"wifi:{wifi['id']}", generate_key(24))
            keep = netplan.secret_refs(plan) | (netplan.secret_refs(self.applied) if self.applied else set())
            self._secrets = {ref: token for ref, token in self._secrets.items() if ref in keep}
            self.plan = plan
            self._log("plan_saved", f"{len(plan['networks'])} networks, {len(plan['wifi'])} Wi-Fi, {len(plan['keys'])} keys", user)
            await self._async_save()
        return netplan.validate(plan, self.secret)

    # ------------------------------------------------------------------ devices

    def role(self, coordinator: AerohiveCoordinator, plan: dict[str, Any] | None = None) -> str:
        plan = plan or self.plan
        data = coordinator.data
        if data is None:
            return ROLE_NONE
        entry_id = coordinator.config_entry.entry_id
        aps = plan["devices"].get("aps")
        ap_ok = aps is None or entry_id in aps
        if coordinator.is_openwrt:
            if entry_id == plan["devices"].get("router"):
                return ROLE_ROUTER
            return ROLE_AP if data.is_wireless and ap_ok else ROLE_NONE
        if data.cli in (commands.DIALECT_EXOS, commands.DIALECT_SWITCH):
            return ROLE_SWITCH
        if data.cli == commands.DIALECT_HIVEOS and data.is_wireless and ap_ok:
            return ROLE_AP
        return ROLE_NONE

    def devices(self) -> list[dict[str, Any]]:
        result = []
        for c in _coordinators(self.hass):
            data = c.data
            kind = "openwrt" if c.is_openwrt else (data.cli if data else None)
            note = None
            if data is not None and data.cli == commands.DIALECT_SWITCH:
                note = "VLAN commands for SR switches are assumed until confirmed on a real SR2208P (shown in the review)"
            elif data is not None and data.cli == commands.DIALECT_HIVEOS and not data.is_wireless:
                note = "No Wi-Fi radios"
            result.append(
                {
                    "entry_id": c.config_entry.entry_id,
                    "name": (data.hostname if data else None) or c.config_entry.title,
                    "host": c.client.host,
                    "kind": kind,
                    "online": bool(c.last_update_success and data is not None),
                    "role": self.role(c),
                    "wireless": bool(data and data.is_wireless),
                    "router_capable": c.is_openwrt,
                    "note": note,
                }
            )
        return result

    def _build(
        self, coordinator: AerohiveCoordinator, plan: dict[str, Any], *, wireless_only: bool = False
    ) -> tuple[str, netplan.DevicePlan]:
        role = self.role(coordinator, plan)
        entry_id = coordinator.config_entry.entry_id
        managed = self.managed.get(entry_id, {})
        data = coordinator.data
        if role == ROLE_NONE:
            return role, netplan.DevicePlan()
        try:
            if coordinator.is_openwrt:
                return role, netplan.openwrt_lines(
                    plan,
                    self.secret,
                    is_router=role == ROLE_ROUTER,
                    radios=radio_bands(coordinator),
                    now=dt_util.utcnow(),
                    wireless_only=wireless_only,
                )
            if role == ROLE_SWITCH and data.cli == commands.DIALECT_SWITCH:
                existing = {v.name: v.vid for v in data.vlans.values()}
                ports = [p for p in data.ports() if not p.startswith("0/")]
                return role, netplan.sr_commands(plan, entry_id=entry_id, existing=existing, ports=ports, managed=managed.get("vlans", []))
            if role == ROLE_SWITCH:
                existing = {v.name: v.vid for v in data.vlans.values()}
                return role, netplan.exos_commands(plan, entry_id=entry_id, existing=existing, managed=managed.get("vlans", []))
            radios = radio_bands(coordinator)
            bound = {ssid: {r for r in radios if data.ssid_on_radio(ssid, r)} for ssid in data.ssids}
            objects = dict(data.config.ssid_security_objects) if data.config else {}
            return role, netplan.hiveos_commands(
                plan, self.secret, radios=radios, bound=bound, security_objects=objects, managed=managed.get("ssids", [])
            )
        except (commands.InvalidArgument, ValueError) as err:
            result = netplan.DevicePlan()
            result.errors.append(str(err))
            return role, result

    @staticmethod
    def _scrub(text: str, secrets: list[str]) -> str:
        text = commands.redact(text)
        for value in secrets:
            if value:
                text = text.replace(value, "***")
        return text

    def preview(self) -> dict[str, Any]:
        errors = netplan.validate(self.plan, self.secret)
        devices = []
        for c in sorted(_coordinators(self.hass), key=lambda c: _ORDER.get(self.role(c), 9)):
            role, built = self._build(c, self.plan)
            if role == ROLE_NONE:
                continue
            devices.append(
                {
                    "entry_id": c.config_entry.entry_id,
                    "name": (c.data.hostname if c.data else None) or c.config_entry.title,
                    "role": role,
                    "online": bool(c.last_update_success and c.data is not None),
                    "commands": [self._scrub(cmd, built.secrets) for cmd in built.commands],
                    "warnings": built.warnings,
                    "errors": built.errors,
                }
            )
        notes = []
        if not self.plan["devices"].get("router") and self.plan["networks"]:
            notes.append(
                "No OpenWrt router chosen: the VLANs are set up on switches and access points, but addresses, "
                "DHCP and firewall rules must come from your own router"
            )
        return {"errors": errors, "notes": notes, "devices": devices}

    # ------------------------------------------------------------------ applying

    async def _apply_device(
        self, coordinator: AerohiveCoordinator, plan: dict[str, Any], *, wireless_only: bool = False
    ) -> dict[str, Any]:
        entry_id = coordinator.config_entry.entry_id
        name = (coordinator.data.hostname if coordinator.data else None) or coordinator.config_entry.title
        role, built = self._build(coordinator, plan, wireless_only=wireless_only)
        result = {"entry_id": entry_id, "name": name, "role": role, "ok": False, "error": None, "warnings": built.warnings}
        if built.errors:
            result["error"] = "; ".join(built.errors)
            return result
        if not (coordinator.last_update_success and coordinator.data is not None):
            result["error"] = "offline"
            return result
        if built.groups and not coordinator.is_openwrt:
            # One part per Wi-Fi network: a rejected one doesn't stop the others.
            done, failed = [], []
            for label, cmds in built.groups:
                if not cmds:
                    done.append(label)
                    continue
                try:
                    await coordinator.async_apply_cli_plan(cmds, built.secrets, refresh=False)
                except HomeAssistantError as err:
                    failed.append(f"{label}: {self._scrub(str(err), built.secrets)}")
                else:
                    done.append(label)
            await coordinator.async_request_refresh()
            ok_ssids = [s for s in built.managed.get("ssids", []) if s in done]
            previous = self.managed.get(entry_id, {}).get("ssids", [])
            removed = {label.removeprefix("remove ") for label in done if label.startswith("remove ")}
            # Created now, or earlier and not removed now (a network that failed this time stays ours).
            self.managed[entry_id] = {**self.managed.get(entry_id, {}), "ssids": sorted((set(previous) - removed) | set(ok_ssids))}
            result["done"] = done
            if failed:
                result["error"] = "; ".join(failed) + (f" (done: {', '.join(done)})" if done else "")
                return result
            result["ok"] = True
            return result
        try:
            if coordinator.is_openwrt:
                configs = ("wireless",) if wireless_only else netplan.OPENWRT_CONFIGS
                await coordinator.async_apply_openwrt_plan(built.commands, built.secrets, configs)
            else:
                await coordinator.async_apply_cli_plan(built.commands, built.secrets)
        except HomeAssistantError as err:
            result["error"] = self._scrub(str(err), built.secrets)
            return result
        if built.managed:
            self.managed[entry_id] = {**self.managed.get(entry_id, {}), **built.managed}
        if role == ROLE_ROUTER and not wireless_only:
            try:
                await self._async_install_portal(coordinator, plan)
            except HomeAssistantError as err:
                result["error"] = f"Network applied, but the onboarding portal failed: {err}"
                return result
        result["ok"] = True
        return result

    # ------------------------------------------------------------------ onboarding portal

    def _router(self, plan: dict[str, Any] | None = None) -> AerohiveCoordinator | None:
        plan = plan or self.applied or self.plan
        router_id = plan["devices"].get("router") if plan else None
        for c in _coordinators(self.hass):
            if c.config_entry.entry_id == router_id and c.is_openwrt and c.data is not None and c.last_update_success:
                return c
        return None

    def _portal_offer(self, plan: dict[str, Any]) -> list[tuple[str, str]]:
        offer = plan["onboarding"].get("offer") or []
        return [(w["id"], w["ssid"]) for w in plan["wifi"] if w["id"] in offer and w["security"] == "ppsk"]

    async def _async_install_portal(self, coordinator: AerohiveCoordinator, plan: dict[str, Any]) -> None:
        address = netplan.portal_address(plan)
        if address is None:
            await coordinator.async_run_router_lines([*portal.remove_lines(), portal.UHTTPD_RELOAD])
            return
        check = await coordinator.async_run_router_lines([portal.UHTTPD_CHECK])
        if "HN_UHTTPD_OK" not in check:
            raise HomeAssistantError("the router's web server (uhttpd) isn't installed: opkg install uhttpd")
        onboarding = plan["onboarding"]
        lines = portal.install_lines(address=address, look=portal.look_from(onboarding), offer=self._portal_offer(plan))
        await coordinator.async_run_router_lines([*lines, portal.UHTTPD_RELOAD])

    def _portal_on(self) -> bool:
        return bool(self.applied and netplan.onboarding_network(self.applied) is not None)

    async def _async_portal_tick(self, _now: Any = None) -> None:
        """Read new requests from the router; tidy up old ones."""
        router = self._router()
        if not self._portal_on() or router is None:
            return
        try:
            out = await router.async_run_router_lines([portal.LIST_REQUESTS])
        except HomeAssistantError as err:
            self.portal_error = str(err)
            return
        self.portal_error = None
        now = dt_util.utcnow()
        seen = {r.id: r for r in portal.parse_requests(out)}
        changed = False
        new: list[str] = []
        for rid, req in seen.items():
            known = self.requests.get(rid)
            if known and known.get("token") == req.token:
                continue
            item = {**portal.as_dict(req), "token": req.token, "status": "pending", "seen": now.isoformat()}
            if rule := self._rule_for(item["kind"]):
                item["suggested"] = {"wifi": rule.get("wifi"), "network": rule.get("network"), "minutes": rule.get("minutes"), "auto": bool(rule.get("auto"))}
            self.requests[rid] = item
            new.append(rid)
            changed = True
        stale = []
        for rid, item in self.requests.items():
            age = now - dt_util.parse_datetime(item.get("decided") or item["seen"])
            if item["status"] == "pending" and rid not in seen:
                stale.append(rid)  # gone from the router (it restarted, or it was removed there)
            elif age > REQUEST_TTL:
                stale.append(rid)
        for rid in stale:
            item = self.requests.pop(rid)
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_onboard_{rid}")
            changed = True
            try:
                await router.async_run_router_lines(portal.forget_lines(rid))
            except HomeAssistantError:
                pass
            if item["status"] == "pending":
                self._log("onboarding_expired", item.get("name") or rid)
        if changed:
            await self._async_save()
        for rid in new:
            item = self.requests[rid]
            suggested = item.get("suggested") or {}
            if suggested.get("auto"):
                try:
                    await self.async_approve(
                        rid, wifi=suggested["wifi"], network=suggested.get("network"), minutes=suggested.get("minutes"),
                        user=f"rule for {self.kind_label(item['kind'])}",
                    )
                except HomeAssistantError as err:
                    _LOGGER.warning("Approving %s by rule failed: %s", item["name"], err)
                else:
                    self._fire_request(item, auto=True)
                    continue
            self._announce(item)

    def _rule_for(self, kind: str) -> dict[str, Any] | None:
        plan = self.applied or self.plan
        return next((r for r in plan["onboarding"].get("rules") or [] if r.get("kind") == kind), None)

    def default_wifi(self, item: dict[str, Any]) -> str | None:
        """The Wi-Fi a request gets unless told otherwise: its rule, what it asked for, else the first offered."""
        offered = [w["id"] for w in self.plan["wifi"] if w["security"] == "ppsk" and w["id"] in (self.plan["onboarding"].get("offer") or [])]
        for choice in ((item.get("suggested") or {}).get("wifi"), item.get("wants")):
            if choice and any(w["id"] == choice and w["security"] == "ppsk" for w in self.plan["wifi"]):
                return choice
        return offered[0] if offered else None

    def _fire_request(self, item: dict[str, Any], *, auto: bool = False) -> None:
        suggested = item.get("suggested") or {}
        self.hass.bus.async_fire(
            EVENT_ONBOARDING,
            {
                **{k: item.get(k) for k in ("id", "name", "kind", "mac", "ip", "wants")},
                "kind_label": self.kind_label(item["kind"]),
                "suggested_wifi": suggested.get("wifi") or self.default_wifi(item),
                "auto_approved": auto,
            },
        )

    def _announce(self, item: dict[str, Any]) -> None:
        kind = self.kind_label(item["kind"])
        wants = next((w["ssid"] for w in self.plan["wifi"] if w["id"] == item.get("wants")), None)
        persistent_notification.async_create(
            self.hass,
            f"**{item['name']}** ({kind}, {item['mac']}) asks to join"
            + (f" **{wants}**" if wants else " the network")
            + ". [Approve or decline it](/aerohive-network/setup).",
            title="New device waiting for approval",
            notification_id=f"{DOMAIN}_onboard_{item['id']}",
        )
        self._fire_request(item)
        self._log("onboarding_request", f"{item['name']} ({kind})")

    def kind_label(self, kind_id: str) -> str:
        return dict(portal.kind_choices(self.plan["onboarding"].get("kinds"))).get(kind_id, kind_id)

    def portal_preview(self, settings: dict[str, Any], page: str) -> str:
        """The portal page as a device would see it, from (unsaved) onboarding settings."""
        merged = netplan.normalize({**self.plan, "onboarding": {**self.plan["onboarding"], **settings}})
        look = portal.look_from(merged["onboarding"])
        offer = self._portal_offer(merged) or [("example", "Example Wi-Fi")]
        svg = qr.svg(qr.wifi_payload(offer[0][1], "Kx7mPq2vR9tw", "wpa2")) if page == "approved" else None
        return portal.preview(look, offer, page, qr_svg=svg)

    def _pending_request(self, request_id: str) -> dict[str, Any]:
        item = self.requests.get(request_id)
        if item is None or item.get("status") != "pending":
            raise HomeAssistantError("That request is no longer waiting")
        return item

    async def async_approve(
        self,
        request_id: str,
        *,
        wifi: str,
        network: str | None = None,
        minutes: int | None = None,
        bind_mac: bool = False,
        user: str | None = None,
    ) -> dict[str, Any]:
        item = self._pending_request(request_id)
        router = self._router()
        if router is None:
            raise HomeAssistantError("The router isn't reachable")
        added = await self.async_add_key(
            name=item["name"], wifi=wifi, network=network, minutes=minutes, mac=item["mac"] if bind_mac else None, user=user
        )
        key_id = added["id"]
        value = self.secret(f"key:{key_id}") or ""
        target = next(w for w in self.plan["wifi"] if w["id"] == wifi)
        svg = qr.svg(qr.wifi_payload(target["ssid"], value, target["security"], bool(target.get("hidden"))))
        page = portal.approved_page(
            look=portal.look_from(self.plan["onboarding"]),
            name=item["name"],
            ssid=target["ssid"],
            key=value,
            qr_svg=svg,
            expires_text=f"This password works for {minutes} minutes." if minutes else None,
        )
        try:
            await router.async_run_router_lines(portal.approve_lines(request_id, item["token"], page), [value])
        except HomeAssistantError as err:
            raise HomeAssistantError(f"The key was created, but the portal couldn't show it: {err}") from err
        item.update(status="approved", decided=dt_util.utcnow().isoformat(), key_id=key_id)
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_onboard_{request_id}")
        self._log("onboarding_approved", f"{item['name']} → {target['ssid']}", user)
        await self._async_save()
        return {"key_id": key_id, "results": added["results"]}

    async def async_decline(self, request_id: str, user: str | None = None) -> None:
        item = self._pending_request(request_id)
        router = self._router()
        if router is None:
            raise HomeAssistantError("The router isn't reachable")
        await router.async_run_router_lines(portal.decline_lines(request_id))
        item.update(status="declined", decided=dt_util.utcnow().isoformat())
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_onboard_{request_id}")
        self._log("onboarding_declined", item["name"], user)
        await self._async_save()

    async def async_apply(self, entry_ids: list[str] | None = None, user: str | None = None) -> list[dict[str, Any]]:
        errors = netplan.validate(self.plan, self.secret)
        if errors:
            raise HomeAssistantError("Fix these first: " + "; ".join(errors[:5]))
        async with self._lock:
            plan = copy.deepcopy(self.plan)
            targets = [c for c in _coordinators(self.hass) if self.role(c, plan) != ROLE_NONE]
            if entry_ids:
                targets = [c for c in targets if c.config_entry.entry_id in entry_ids]
            if not targets:
                raise HomeAssistantError("No device to apply the setup to")
            results: list[dict[str, Any]] = []
            for order in sorted({_ORDER[self.role(c, plan)] for c in targets}):
                group = [c for c in targets if _ORDER[self.role(c, plan)] == order]
                results += await asyncio.gather(*(self._apply_device(c, plan) for c in group))
            if all(r["ok"] for r in results):
                self.applied = plan
                self.applied_at = dt_util.utcnow().isoformat()
                self.needs_apply -= {r["entry_id"] for r in results}
            ok = [r["name"] for r in results if r["ok"]]
            failed = [f"{r['name']}: {r['error']}" for r in results if not r["ok"]]
            self._log("applied", f"ok: {', '.join(ok) or '-'}" + (f"; failed: {'; '.join(failed)}" if failed else ""), user)
            self.last_results = results
            await self._async_save()
        return results

    def pending(self) -> bool:
        if self.applied is None:
            return bool(self.plan["networks"] or self.plan["wifi"])
        strip = lambda p: {k: v for k, v in p.items() if k != "keys"}  # noqa: E731 - keys apply on their own
        return strip(self.plan) != strip(self.applied)

    # ------------------------------------------------------------------ keys

    def _key_targets(self, wifi_ids: set[str]) -> list[AerohiveCoordinator]:
        """Devices that broadcast these Wi-Fi networks with per-device keys (OpenWrt)."""
        if self.applied is None:
            return []
        if not any(w["id"] in wifi_ids and w["security"] == "ppsk" for w in self.applied["wifi"]):
            return []
        return [
            c for c in _coordinators(self.hass)
            if c.is_openwrt and c.data is not None and c.data.is_wireless and self.role(c, self.applied) != ROLE_NONE
        ]

    async def _async_push_keys(self, coordinators: list[AerohiveCoordinator]) -> list[dict[str, Any]]:
        """Send the current keys to devices using the layout as last applied."""
        if self.applied is None or not coordinators:
            return []
        plan = {**copy.deepcopy(self.applied), "keys": copy.deepcopy(self.plan["keys"])}
        results = await asyncio.gather(*(self._apply_device(c, plan, wireless_only=True) for c in coordinators))
        for result in results:
            if result["ok"]:
                self.needs_apply.discard(result["entry_id"])
            else:
                self.needs_apply.add(result["entry_id"])
        return list(results)

    async def async_add_key(
        self,
        *,
        name: str,
        wifi: str,
        network: str | None = None,
        mac: str | None = None,
        minutes: int | None = None,
        key: str | None = None,
        rotate_days: int | None = None,
        cutoff_days: int | None = None,
        user: str | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise HomeAssistantError("Give the device a name")
        target = next((w for w in self.plan["wifi"] if w["id"] == wifi), None)
        if target is None or target["security"] != "ppsk":
            raise HomeAssistantError("Choose a Wi-Fi network that uses per-device keys")
        value = key or generate_key()
        try:
            commands.validate_psk(value)
        except commands.InvalidArgument as err:
            raise HomeAssistantError(str(err)) from err
        if any(self.secret(f"key:{k['id']}") == value for k in self.plan["keys"] if k["wifi"] == wifi):
            raise HomeAssistantError("Another device already uses this key on that network")
        async with self._lock:
            base = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")[:10] or "key"
            if not base[0].isalpha():
                base = f"k{base}"[:10]
            kid, n = base, 2
            existing = {k["id"] for k in self.plan["keys"]}
            while kid in existing:
                kid, n = f"{base}{n}", n + 1
            now = dt_util.utcnow()
            item = {
                "id": kid,
                "name": name,
                "wifi": wifi,
                "network": network or None,
                "mac": (mac or "").lower() or None,
                "expires": (now + timedelta(minutes=minutes)).isoformat() if minutes else None,
                "created": now.isoformat(),
                "rotate_days": rotate_days or None,
                "cutoff_days": cutoff_days or None,
            }
            plan = netplan.normalize({**self.plan, "keys": [*self.plan["keys"], item]})
            self._set_secret(f"key:{kid}", value)
            problems = [e for e in netplan.validate(plan, self.secret) if name in e or kid in e]
            if problems:
                self._set_secret(f"key:{kid}", None)
                raise HomeAssistantError("; ".join(problems))
            self.plan = plan
            self._log("key_added", f"{name} on {target['ssid']}" + (f" for {minutes} min" if minutes else ""), user)
            results = await self._async_push_keys(self._key_targets({wifi}))
            await self._async_save()
        return {"id": kid, "results": results, "applied": self.applied is not None}

    async def async_revoke_key(self, key_id: str, user: str | None = None, reason: str = "revoked") -> list[dict[str, Any]]:
        async with self._lock:
            item = next((k for k in self.plan["keys"] if k["id"] == key_id), None)
            if item is None:
                raise HomeAssistantError("That key doesn't exist")
            self.plan = {**self.plan, "keys": [k for k in self.plan["keys"] if k["id"] != key_id]}
            self._set_secret(f"key:{key_id}", None)
            self._set_secret(f"key:{key_id}:next", None)
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_rotation_{key_id}")
            self._log(f"key_{reason}", item.get("name") or key_id, user)
            results = await self._async_push_keys(self._key_targets({item["wifi"]}))
            await self._async_save()
        return results

    # ------------------------------------------------------------------ key rotation

    def _rotation_note(self, item: dict[str, Any]) -> None:
        ssid = next((w["ssid"] for w in self.plan["wifi"] if w["id"] == item["wifi"]), item["wifi"])
        cutoff = (item.get("rotation") or {}).get("cutoff")
        until = f"until {dt_util.as_local(dt_util.parse_datetime(cutoff)).strftime('%d %b %Y')}" if cutoff else "until you confirm"
        persistent_notification.async_create(
            self.hass,
            f"**{item['name']}** has a new key for **{ssid}**. Put it on the device, then press *Device updated* "
            f"in [Setup → Devices & keys](/aerohive-network/setup). The old key keeps working {until}.",
            title="Wi-Fi key to update",
            notification_id=f"{DOMAIN}_rotation_{item['id']}",
        )
        self.hass.bus.async_fire(EVENT_ROTATION, {"key_id": item["id"], "name": item["name"], "wifi": item["wifi"], "ssid": ssid, "cutoff": cutoff})

    async def async_rotate_keys(
        self, key_ids: list[str], *, cutoff_days: int | None = None, user: str | None = None, reason: str = "manual"
    ) -> list[dict[str, Any]]:
        """Start rotating keys: each gets a new key that works alongside the old one.

        The old key stops when the rotation is finished (``async_finish_rotation``,
        e.g. after the device was updated) or at the cutoff, if there is one.
        """
        async with self._lock:
            now = dt_util.utcnow()
            started = []
            for item in self.plan["keys"]:
                if item["id"] not in key_ids or item.get("rotation"):
                    continue
                days = cutoff_days if cutoff_days is not None else item.get("cutoff_days")
                self._set_secret(f"key:{item['id']}:next", generate_key())
                item["rotation"] = {"started": now.isoformat(), "cutoff": (now + timedelta(days=days)).isoformat() if days else None}
                started.append(item)
            if not started and key_ids:
                raise HomeAssistantError("Those keys are already being rotated" if any(k["id"] in key_ids for k in self.plan["keys"]) else "No such key")
            for item in started:
                self._rotation_note(item)
                self._log("key_rotation_started", f"{item['name']}" + (" (scheduled)" if reason == "scheduled" else ""), user)
            results = await self._async_push_keys(self._key_targets({k["wifi"] for k in started}))
            await self._async_save()
        return results

    async def async_finish_rotation(self, key_id: str, *, user: str | None = None, reason: str = "confirmed") -> list[dict[str, Any]]:
        """The new key becomes the key; the old one stops working."""
        async with self._lock:
            item = next((k for k in self.plan["keys"] if k["id"] == key_id), None)
            if item is None or not item.get("rotation"):
                raise HomeAssistantError("That key isn't being rotated")
            upcoming = self.secret(f"key:{key_id}:next")
            if upcoming:
                self._set_secret(f"key:{key_id}", upcoming)
            self._set_secret(f"key:{key_id}:next", None)
            item["rotation"] = None
            item["rotated"] = dt_util.utcnow().isoformat()
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_rotation_{key_id}")
            self._log("key_rotated", f"{item['name']}" + (" (cutoff reached)" if reason == "cutoff" else ""), user)
            results = await self._async_push_keys(self._key_targets({item["wifi"]}))
            await self._async_save()
        return results

    async def async_cancel_rotation(self, key_id: str, *, user: str | None = None) -> list[dict[str, Any]]:
        """Keep the old key; the new one stops working."""
        async with self._lock:
            item = next((k for k in self.plan["keys"] if k["id"] == key_id), None)
            if item is None or not item.get("rotation"):
                raise HomeAssistantError("That key isn't being rotated")
            self._set_secret(f"key:{key_id}:next", None)
            item["rotation"] = None
            item["rotated"] = dt_util.utcnow().isoformat()  # don't start again straight away
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_rotation_{key_id}")
            self._log("key_rotation_cancelled", item["name"], user)
            results = await self._async_push_keys(self._key_targets({item["wifi"]}))
            await self._async_save()
        return results

    async def _async_rotation_tick(self, now: Any) -> None:
        """Start scheduled rotations; finish rotations whose cutoff passed."""
        due = []
        for item in self.plan["keys"]:
            rotation = item.get("rotation")
            if rotation:
                if rotation.get("cutoff") and dt_util.parse_datetime(rotation["cutoff"]) <= now:
                    try:
                        await self.async_finish_rotation(item["id"], reason="cutoff")
                    except HomeAssistantError as err:
                        _LOGGER.warning("Finishing the key rotation of %s failed: %s", item.get("name"), err)
                continue
            if item.get("rotate_days") and not item.get("expires"):
                since = dt_util.parse_datetime(item.get("rotated") or item.get("created") or now.isoformat())
                if since and since + timedelta(days=item["rotate_days"]) <= now:
                    due.append(item["id"])
        if due:
            try:
                await self.async_rotate_keys(due, reason="scheduled")
            except HomeAssistantError as err:
                _LOGGER.warning("Starting scheduled key rotations failed: %s", err)

    async def _async_tick(self, _now: Any = None) -> None:
        now = dt_util.utcnow()
        await self._async_rotation_tick(now)
        expired = netplan.expired_keys(self.plan, now)
        for item in expired:
            try:
                await self.async_revoke_key(item["id"], reason="expired")
            except HomeAssistantError as err:
                _LOGGER.warning("Removing expired key %s failed: %s", item.get("name"), err)
        if self.needs_apply and not self._lock.locked():
            retry = [c for c in _coordinators(self.hass) if c.config_entry.entry_id in self.needs_apply]
            if retry:
                async with self._lock:
                    await self._async_push_keys(retry)
                    await self._async_save()

    # ------------------------------------------------------------------ for the panel

    def state(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "secrets": self.secrets_set(),
            "devices": self.devices(),
            "errors": netplan.validate(self.plan, self.secret),
            "pending": self.pending(),
            "applied_at": self.applied_at,
            "last_results": self.last_results,
            "needs_apply": sorted(self.needs_apply),
            "audit": self.audit[-50:][::-1],
            "now": dt_util.utcnow().isoformat(),
            "portal": {
                "on": self._portal_on(),
                "url": f"http://{netplan.portal_address(self.applied)}:{portal.PORT}/" if self._portal_on() else None,
                "error": self.portal_error,
            },
            "requests": sorted(
                ({**{k: v for k, v in r.items() if k != "token"}, "kind_label": self.kind_label(r["kind"])} for r in self.requests.values()),
                key=lambda r: r["seen"],
                reverse=True,
            ),
        }


async def async_setup_plan(hass: HomeAssistant) -> PlanManager:
    manager = hass.data.get(DATA_PLAN)
    if manager is None:
        manager = PlanManager(hass)
        await manager.async_load()
        hass.data[DATA_PLAN] = manager
    return manager


def get_plan(hass: HomeAssistant) -> PlanManager:
    manager = hass.data.get(DATA_PLAN)
    if manager is None:
        raise HomeAssistantError("The network setup isn't loaded yet")
    return manager
