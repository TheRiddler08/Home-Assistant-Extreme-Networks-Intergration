"""Shared entity helpers for Aerohive Local."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONTROL_DEBOUNCE_SECONDS
from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData


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


def async_add_dynamic_entities(
    coordinator: AerohiveCoordinator,
    entry: AerohiveConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[AerohiveData], Iterable[tuple[str, Callable[[], Entity]]]],
) -> None:
    """Add entities now and whenever new keys (SSIDs, radios...) show up."""
    known: set[str] = set()

    @callback
    def _check() -> None:
        if coordinator.data is None:
            return
        new: list[Entity] = []
        for key, build in factory(coordinator.data):
            if key in known:
                continue
            known.add(key)
            new.append(build())
        if new:
            async_add_entities(new)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))


def radio_label(data: AerohiveData, radio: str) -> str:
    """'wifi0 (2.4 GHz)'."""
    from .parsers import radio_band  # local import keeps module graph simple

    iface = data.interfaces.get(radio)
    band = radio_band(iface.radio_profile if iface else None, iface.channel if iface else None)
    return f"{radio} ({band})" if band else radio


def extra_attrs(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
