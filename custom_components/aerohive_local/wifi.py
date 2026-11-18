"""Wi-Fi networks across access points, and timed (temporary) changes.

A network is an SSID name; it can exist on several access points. Changes
apply to every online AP that has it unless specific APs are chosen.

A timed change ("turn Guest off for 2 hours") is remembered in Home
Assistant's storage, so it's reverted even if Home Assistant restarts in
between. What's restored is exactly what was there before: the radios each AP
broadcast the network on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import commands
from .const import DOMAIN
from .parsers import radio_band

if TYPE_CHECKING:
    from .coordinator import AerohiveCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.wifi_timers"
STORAGE_VERSION = 1
DATA_TIMERS = f"{DOMAIN}_wifi_timers"
SIGNAL_SNAPSHOT = f"{DOMAIN}_snapshot_changed"  # same signal as websocket_api (no import cycle)
RETRY_SECONDS = 60
MAX_DURATION = timedelta(days=7)


# --------------------------------------------------------------------------
# Finding the access points
# --------------------------------------------------------------------------


def access_points(hass: HomeAssistant) -> list[AerohiveCoordinator]:
    """Loaded, reachable access points (and branch routers with Wi-Fi)."""
    from .coordinator import AerohiveCoordinator  # noqa: PLC0415 - avoid an import cycle

    result = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if (
            entry.state is ConfigEntryState.LOADED
            and isinstance(coordinator, AerohiveCoordinator)
            and coordinator.data is not None
            and coordinator.last_update_success
            and coordinator.data.is_wireless
        ):
            result.append(coordinator)
    return result


def _pick(hass: HomeAssistant, entry_ids: list[str] | None, ssid: str | None = None, *, must_have: bool = True):
    aps = access_points(hass)
    if entry_ids:
        aps = [ap for ap in aps if ap.config_entry.entry_id in entry_ids]
    if ssid is not None and must_have:
        aps = [ap for ap in aps if ssid in ap.data.ssids]
    return aps


def radios_for_bands(coordinator: AerohiveCoordinator, bands: list[str] | None) -> list[str]:
    """The AP's radios on the given bands ('2.4 GHz', '5 GHz'); all radios for None."""
    data = coordinator.data
    radios = data.radio_names()
    if bands is None:
        return radios
    wanted = {b.replace(" ", "").lower() for b in bands}
    result = []
    for radio in radios:
        iface = data.interfaces.get(radio)
        band = radio_band(iface.radio_profile if iface else None, iface.channel if iface else None)
        if band and band.replace(" ", "").lower() in wanted:
            result.append(radio)
    return result


async def _fan_out(aps: list[AerohiveCoordinator], what: str, action) -> None:  # type: ignore[no-untyped-def]
    """Run an action on several APs at once; report every failure together."""
    if not aps:
        raise HomeAssistantError(f"No reachable access point to {what}")
    results = await asyncio.gather(*(action(ap) for ap in aps), return_exceptions=True)
    failed = [f"{ap.data.hostname or ap.client.host}: {r}" for ap, r in zip(aps, results, strict=True) if isinstance(r, Exception)]
    if failed:
        raise HomeAssistantError(f"Could not {what} on " + "; ".join(failed))


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------


def _validate_password(password: str | None) -> None:
    if password:
        try:
            commands.validate_psk(password)
        except commands.InvalidArgument as err:
            raise HomeAssistantError(str(err)) from err


async def async_update_network(
    hass: HomeAssistant,
    ssid: str,
    *,
    entry_ids: list[str] | None = None,
    enabled: bool | None = None,
    bands: list[str] | None = None,
    hidden: bool | None = None,
    max_clients: int | None = None,
    password: str | None = None,
    duration: timedelta | None = None,
) -> None:
    """Change a network on its access points.

    enabled=True broadcasts it on ``bands`` (default: all radios), False on none.
    bands alone changes which bands it's on. duration makes enabled temporary.
    """
    _validate_password(password)
    aps = _pick(hass, entry_ids, ssid)
    if not aps:
        raise HomeAssistantError(f"No reachable access point has the network {ssid}")
    timers = get_timers(hass)
    if duration is not None:
        if enabled is None:
            raise HomeAssistantError("A duration needs enabled: true or false")
        if not timedelta(minutes=1) <= duration <= MAX_DURATION:
            raise HomeAssistantError("The duration must be between 1 minute and 7 days")
        before = {ap.config_entry.entry_id: [r for r in ap.data.radio_names() if ap.data.ssid_on_radio(ssid, r)] for ap in aps}
    elif enabled is not None:
        await timers.async_cancel(ssid, revert=False)  # a manual on/off replaces a timer

    async def one(ap: AerohiveCoordinator) -> None:
        radios = None
        if enabled is not None:
            if enabled and bands is None:
                radios = ap.wifi_home_radios(ssid) or radios_for_bands(ap, None)
            else:
                radios = radios_for_bands(ap, bands) if enabled else []
            if enabled and not radios:
                raise HomeAssistantError(f"no radio on {', '.join(bands or [])}")
        elif bands is not None:
            radios = radios_for_bands(ap, bands)
        await ap.async_update_ssid(ssid, password=password, radios=radios, hidden=hidden, max_clients=max_clients)

    await _fan_out(aps, f"change {ssid}", one)
    if duration is not None:
        await timers.async_add(ssid, before, duration, enabled=bool(enabled))


async def async_create_network(
    hass: HomeAssistant,
    ssid: str,
    *,
    password: str | None = None,
    bands: list[str] | None = None,
    hidden: bool = False,
    entry_ids: list[str] | None = None,
) -> None:
    try:
        commands.validate_ssid_name(ssid)
    except commands.InvalidArgument as err:
        raise HomeAssistantError(str(err)) from err
    _validate_password(password)
    aps = [ap for ap in _pick(hass, entry_ids, ssid, must_have=False) if ssid not in ap.data.ssids]
    if not aps:
        raise HomeAssistantError(f"{ssid} already exists on the selected access points" if access_points(hass) else "No reachable access point")

    async def one(ap: AerohiveCoordinator) -> None:
        radios = radios_for_bands(ap, bands)
        if not radios:
            raise HomeAssistantError(f"no radio on {', '.join(bands or [])}")
        await ap.async_create_ssid(ssid, password, radios, hidden)

    await _fan_out(aps, f"create {ssid}", one)


async def async_delete_network(hass: HomeAssistant, ssid: str, *, entry_ids: list[str] | None = None) -> None:
    aps = _pick(hass, entry_ids, ssid)
    await get_timers(hass).async_cancel(ssid, revert=False)
    await _fan_out(aps, f"delete {ssid}", lambda ap: ap.async_delete_ssid(ssid))


# --------------------------------------------------------------------------
# Timed changes
# --------------------------------------------------------------------------


@dataclass
class WifiTimer:
    ssid: str
    until: datetime
    enabled: bool  # the temporary state
    restore: dict[str, list[str]]  # entry_id -> radios to broadcast on afterwards

    def as_dict(self) -> dict[str, Any]:
        return {"ssid": self.ssid, "until": self.until.isoformat(), "enabled": self.enabled, "restore": self.restore}


class WifiTimers:
    """Persisted temporary changes, reverted on time (also after a restart)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.timers: dict[str, WifiTimer] = {}
        self._unsub: dict[str, CALLBACK_TYPE] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for item in stored.get("timers", []):
            try:
                timer = WifiTimer(item["ssid"], dt_util.parse_datetime(item["until"]), bool(item["enabled"]), dict(item["restore"]))
            except (KeyError, TypeError, ValueError):
                continue
            if timer.until is None:
                continue
            self.timers[timer.ssid] = timer
            self._schedule(timer)

    def as_list(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in sorted(self.timers.values(), key=lambda t: t.until)]

    async def _save(self) -> None:
        await self._store.async_save({"timers": [t.as_dict() for t in self.timers.values()]})
        async_dispatcher_send(self.hass, SIGNAL_SNAPSHOT)

    def _schedule(self, timer: WifiTimer, when: datetime | None = None) -> None:
        if unsub := self._unsub.pop(timer.ssid, None):
            unsub()
        when = max(when or timer.until, dt_util.utcnow() + timedelta(seconds=1))

        @callback
        def fire(_now: datetime) -> None:
            self._unsub.pop(timer.ssid, None)
            self.hass.async_create_task(self._async_expire(timer.ssid), f"{DOMAIN} wifi timer {timer.ssid}")

        self._unsub[timer.ssid] = async_track_point_in_utc_time(self.hass, fire, when)

    async def async_add(self, ssid: str, restore: dict[str, list[str]], duration: timedelta, *, enabled: bool) -> None:
        existing = self.timers.get(ssid)
        # Changing the time of an existing timer keeps the original state to go back to.
        timer = WifiTimer(ssid, dt_util.utcnow() + duration, enabled, existing.restore if existing else restore)
        self.timers[ssid] = timer
        self._schedule(timer)
        _LOGGER.info("Wi-Fi network %s is %s until %s", ssid, "on" if enabled else "off", timer.until.isoformat())
        await self._save()

    async def async_cancel(self, ssid: str, *, revert: bool) -> None:
        """Forget a timer; revert=True restores the previous state now."""
        if ssid not in self.timers:
            return
        if revert:
            await self._async_expire(ssid)
            return
        self.timers.pop(ssid)
        if unsub := self._unsub.pop(ssid, None):
            unsub()
        await self._save()

    async def _async_expire(self, ssid: str) -> None:
        timer = self.timers.get(ssid)
        if timer is None:
            return
        aps = {ap.config_entry.entry_id: ap for ap in access_points(self.hass)}
        pending: dict[str, list[str]] = {}
        for entry_id, radios in timer.restore.items():
            ap = aps.get(entry_id)
            if ap is None or ssid not in ap.data.ssids:
                if ap is None and self.hass.config_entries.async_get_entry(entry_id) is not None:
                    pending[entry_id] = radios  # not reachable right now: try again
                continue
            try:
                await ap.async_update_ssid(ssid, radios=[r for r in radios if r in ap.data.radio_names()])
            except HomeAssistantError as err:
                _LOGGER.warning("Could not restore Wi-Fi network %s on %s: %s", ssid, ap.client.host, err)
                pending[entry_id] = radios
        if pending and dt_util.utcnow() - timer.until < timedelta(days=1):
            timer.restore = pending
            self._schedule(timer, dt_util.utcnow() + timedelta(seconds=RETRY_SECONDS))
            await self._save()
            return
        self.timers.pop(ssid, None)
        _LOGGER.info("Wi-Fi network %s is back to how it was", ssid)
        await self._save()

    @callback
    def async_shutdown(self) -> None:
        for unsub in self._unsub.values():
            unsub()
        self._unsub.clear()


def get_timers(hass: HomeAssistant) -> WifiTimers:
    timers = hass.data.get(DATA_TIMERS)
    if timers is None:
        timers = hass.data[DATA_TIMERS] = WifiTimers(hass)
    return timers


async def async_setup_timers(hass: HomeAssistant) -> None:
    """Load saved timers. Overdue ones run now and retry every minute until their APs are back."""
    await get_timers(hass).async_load()
