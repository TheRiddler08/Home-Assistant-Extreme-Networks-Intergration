"""Shared entity helpers for Aerohive Local."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONTROL_DEBOUNCE_SECONDS
from .coordinator import AerohiveCoordinator, AerohiveData


class AerohiveEntity(CoordinatorEntity[AerohiveCoordinator]):
    """Base class: attaches entities to the device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AerohiveCoordinator, key: str) -> None:
        super().__init__(coordinator)
        base = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{base}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def entity_registry_enabled_default(self) -> bool:
        # Option "Enable all entities" overrides the per-entity default.
        if self.coordinator.enable_all_entities:
            return True
        return super().entity_registry_enabled_default


class DebouncedSetter[T]:
    """Coalesce rapid control requests; only the last value is applied.

    Every caller awaits; superseded callers return silently once a newer
    request arrives, the final caller runs ``apply`` and sees its errors.
    """

    def __init__(self, apply: Callable[[T], Awaitable[None]], delay: float | None = None) -> None:
        self._apply = apply
        self._delay = CONTROL_DEBOUNCE_SECONDS if delay is None else delay
        self._generation = 0
        self._lock = asyncio.Lock()

    async def async_request(self, value: T) -> None:
        self._generation += 1
        generation = self._generation
        await asyncio.sleep(self._delay)
        if generation != self._generation:
            return
        async with self._lock:
            await self._apply(value)


# Entity families that follow something that can disappear from the device
# (an SSID, or a tracked client). Their entities are removed from Home
# Assistant when it is gone, instead of lingering as "unavailable".
STALE_PREFIXES = ("ssid_", "psk_", "max_client_", "client_")
# A key must be missing from this many consecutive successful polls before its
# entities are removed (protects against one odd/garbled CLI reply). A delete
# made from Home Assistant sets coordinator.expect_removal for an immediate cleanup.
STALE_AFTER_POLLS = 2


def _is_stale_family(key: str) -> bool:
    return key.startswith(STALE_PREFIXES)


def async_add_dynamic_entities(
    coordinator: Any,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[Any], Iterable[tuple[str, Callable[[], Entity]]]],
    unique_base: str | None = None,
) -> None:
    """Add entities when new keys (SSIDs, radios...) show up; remove them when they go away.

    Only keys in STALE_PREFIXES families are ever removed; everything else is
    left alone (it simply shows unavailable if its interface disappears).
    """
    base = unique_base or entry.unique_id or entry.entry_id
    platform = entity_platform.async_get_current_platform()
    domain = platform.domain
    registry = er.async_get(platform.hass)
    known: dict[str, str] = {}  # short key -> unique_id
    misses: dict[str, int] = {}
    startup_cleanup_done = False

    def short(key: str) -> str:
        return key.removeprefix(f"{base}_")

    def remove(unique_id: str) -> None:
        if entity_id := registry.async_get_entity_id(domain, platform.platform_name, unique_id):
            registry.async_remove(entity_id)

    @callback
    def _check() -> None:
        nonlocal startup_cleanup_done
        if coordinator.data is None:
            return
        new: list[Entity] = []
        present: set[str] = set()
        for key, build in factory(coordinator.data):
            key = short(key)
            present.add(key)
            misses.pop(key, None)
            if key in known:
                continue
            entity = build()
            known[key] = entity.unique_id or f"{base}_{key}"
            new.append(entity)
        if new:
            async_add_entities(new)

        if not coordinator.last_update_success:
            return
        family_present = any(_is_stale_family(k) for k in present)
        threshold = 1 if getattr(coordinator, "expect_removal", False) else STALE_AFTER_POLLS

        # Entities whose SSID/client vanished during this run.
        for key in [k for k in known if _is_stale_family(k) and k not in present]:
            misses[key] = misses.get(key, 0) + 1
            if misses[key] >= threshold:
                remove(known.pop(key))
                misses.pop(key, None)

        # Leftovers from earlier runs (e.g. SSID deleted while HA was off).
        # Skipped if this poll returned no SSID/client at all (could be a bad read).
        if not startup_cleanup_done and family_present:
            startup_cleanup_done = True
            for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
                if reg_entry.domain != domain or not reg_entry.unique_id.startswith(f"{base}_"):
                    continue
                key = short(reg_entry.unique_id)
                if _is_stale_family(key) and key not in present:
                    registry.async_remove(reg_entry.entity_id)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))


def radio_label(data: AerohiveData, radio: str) -> str:
    """'wifi0 (2.4 GHz)'."""
    from .parsers import radio_band  # local import keeps module graph simple

    iface = data.interfaces.get(radio)
    band = radio_band(iface.radio_profile if iface else None, iface.channel if iface else None)
    return f"{radio} ({band})" if band else radio


def port_label(name: str) -> str:
    """'1/0/1' -> 'Port 1/0/1', 'eth0' -> 'Eth0'."""
    return f"Port {name}" if name[:1].isdigit() else name.capitalize()


def extra_attrs(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
