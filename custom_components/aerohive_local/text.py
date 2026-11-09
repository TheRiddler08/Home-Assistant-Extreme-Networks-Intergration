"""Per-SSID Wi-Fi key (write-only) text entities for Aerohive Local."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for ssid in data.ssids:
            yield f"psk_{ssid}", lambda ssid=ssid: AerohiveSsidKeyText(coordinator, ssid)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveSsidKeyText(AerohiveEntity, TextEntity):
    """Write-only: setting it changes the WPA2-PSK key.

    The key is never stored in the entity state (state stays unknown), so it
    never reaches the recorder, logbook or logs.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.PASSWORD
    _attr_native_min = 8
    _attr_native_max = 63
    _attr_pattern = r'^[\x20\x21\x23-\x7e]{8,63}$'
    _attr_icon = "mdi:form-textbox-password"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"psk_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} Wi-Fi key"

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    @property
    def native_value(self) -> str | None:
        return None

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_set_ssid_password(self._ssid, value)
