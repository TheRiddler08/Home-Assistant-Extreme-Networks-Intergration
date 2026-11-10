"""Select entities for Aerohive Local: 'SSID to delete' picker."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveData
from .entity import async_add_dynamic_entities
from .overview import async_setup_platform_entities, is_overview
from .ssid_manager import manager_selects

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if is_overview(entry):
        await async_setup_platform_entities("select", hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        if data.radio_names():
            for entity in manager_selects(coordinator, entry.unique_id or entry.entry_id, coordinator.device_info):
                yield entity.unique_id, lambda entity=entity: entity

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)
