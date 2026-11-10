"""On-screen SSID management: type a name/password, press Create; pick, press Delete.

Shared by each AP (AerohiveCoordinator) and the network overview
(OverviewCoordinator), which both provide:
    ssid_draft, ssid_names(), async_create_ssid(name, password, radios),
    async_delete_ssid(name)
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

NO_SELECTION = "—"


class _ManagerEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, unique_base: str, key: str, name: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_base}_{key}"
        self._attr_name = name
        self._device_info = device_info

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class NewSsidNameText(_ManagerEntity, TextEntity):
    _attr_icon = "mdi:wifi-plus"
    _attr_native_min = 0
    _attr_native_max = 32
    _attr_mode = TextMode.TEXT

    @property
    def native_value(self) -> str:
        return self.coordinator.ssid_draft.name

    async def async_set_value(self, value: str) -> None:
        self.coordinator.ssid_draft.name = value.strip()
        self.async_write_ha_state()


class NewSsidPasswordText(_ManagerEntity, TextEntity):
    """Write-only: the typed password is kept in memory, never in the state."""

    _attr_icon = "mdi:form-textbox-password"
    _attr_native_min = 0
    _attr_native_max = 63
    _attr_mode = TextMode.PASSWORD

    @property
    def native_value(self) -> str | None:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"password_entered": bool(self.coordinator.ssid_draft.password)}

    async def async_set_value(self, value: str) -> None:
        self.coordinator.ssid_draft.password = value
        self.async_write_ha_state()


class CreateSsidButton(_ManagerEntity, ButtonEntity):
    _attr_icon = "mdi:wifi-plus"

    async def async_press(self) -> None:
        draft = self.coordinator.ssid_draft
        if not draft.name:
            raise HomeAssistantError("Type a name into 'New SSID name' first")
        await self.coordinator.async_create_ssid(draft.name, draft.password or None, None)
        draft.clear()
        self.coordinator.async_update_listeners()


class SsidDeleteSelect(_ManagerEntity, SelectEntity):
    _attr_icon = "mdi:wifi-remove"

    @property
    def options(self) -> list[str]:
        return [NO_SELECTION, *self.coordinator.ssid_names()]

    @property
    def current_option(self) -> str:
        target = self.coordinator.ssid_draft.delete
        return target if target in self.coordinator.ssid_names() else NO_SELECTION

    async def async_select_option(self, option: str) -> None:
        self.coordinator.ssid_draft.delete = None if option == NO_SELECTION else option
        self.coordinator.async_update_listeners()


class DeleteSsidButton(_ManagerEntity, ButtonEntity):
    _attr_icon = "mdi:delete"

    async def async_press(self) -> None:
        target = self.coordinator.ssid_draft.delete
        if not target or target not in self.coordinator.ssid_names():
            raise HomeAssistantError("Pick the SSID to delete in 'SSID to delete' first")
        await self.coordinator.async_delete_ssid(target)
        self.coordinator.ssid_draft.delete = None
        self.coordinator.async_update_listeners()


def manager_texts(coordinator: Any, unique_base: str, device_info: DeviceInfo, suffix: str = "") -> list[Any]:
    return [
        NewSsidNameText(coordinator, unique_base, "new_ssid_name", f"New SSID name{suffix}", device_info),
        NewSsidPasswordText(coordinator, unique_base, "new_ssid_password", f"New SSID password{suffix}", device_info),
    ]


def manager_buttons(coordinator: Any, unique_base: str, device_info: DeviceInfo, suffix: str = "") -> list[Any]:
    return [
        CreateSsidButton(coordinator, unique_base, "create_ssid", f"Create SSID{suffix}", device_info),
        DeleteSsidButton(coordinator, unique_base, "delete_ssid", f"Delete selected SSID{suffix}", device_info),
    ]


def manager_selects(coordinator: Any, unique_base: str, device_info: DeviceInfo, suffix: str = "") -> list[Any]:
    return [SsidDeleteSelect(coordinator, unique_base, "ssid_delete_target", f"SSID to delete{suffix}", device_info)]
