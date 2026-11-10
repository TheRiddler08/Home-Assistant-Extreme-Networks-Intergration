"""Opt-in device trackers for wireless clients."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TRACKED_CLIENTS
from .coordinator import AerohiveConfigEntry, AerohiveCoordinator
from .entity import extra_attrs
from .overview import is_overview
from .parsers import normalize_mac

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if is_overview(entry):
        return
    coordinator = entry.runtime_data
    macs = {
        mac
        for raw in entry.options.get(CONF_TRACKED_CLIENTS, [])
        if (mac := normalize_mac(raw)) is not None
    }
    async_add_entities(AerohiveClientTracker(coordinator, mac) for mac in sorted(macs))


class AerohiveClientTracker(CoordinatorEntity[AerohiveCoordinator], ScannerEntity):
    """A wireless client seen in 'show station' on this device."""

    _attr_should_poll = False

    def __init__(self, coordinator: AerohiveCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        base = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{base}_tracker_{mac}"
        self._attr_name = f"{coordinator.config_entry.title} client {mac}"

    @property
    def unique_id(self) -> str | None:
        # ScannerEntity would use the bare MAC; prefix it with the AP so the
        # same client can be tracked on several APs without collisions.
        return self._attr_unique_id

    @property
    def entity_registry_enabled_default(self) -> bool:
        # The user explicitly opted in to this MAC in the options flow.
        return True

    @property
    def _station(self):  # type: ignore[no-untyped-def]
        return self.coordinator.data.stations.get(self._mac) if self.coordinator.data else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        if self._station is not None:
            return True
        data = self.coordinator.data
        return data is not None and self._mac in data.wired_client_macs()

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        station = self._station
        return station.ip if station else None

    @property
    def hostname(self) -> str | None:
        station = self._station
        return station.hostname if station else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station
        if station is None:
            wired = [e for e in self.coordinator.data.fdb if e.mac == self._mac] if self.coordinator.data else []
            if not wired:
                return {}
            return extra_attrs(
                connection="wired",
                port=wired[0].port,
                vlan=wired[0].vlan,
                access_point=self.coordinator.config_entry.title,
            )
        return extra_attrs(
            connection="wireless",
            ssid=self.coordinator.data.station_ssid(station),
            interface=station.interface,
            radio=station.radio,
            signal=station.signal,
            snr=station.snr,
            channel=station.channel,
            tx_rate=station.tx_rate,
            rx_rate=station.rx_rate,
            associated_time=station.assoc_time,
            access_point=self.coordinator.config_entry.title,
        )
