"""Config and options flow for Aerohive Local."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import asyncssh
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.components import network
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_create_client
from .client import AerohiveAuthError, AerohiveError
from .const import (
    CONF_DETAILED_STATS,
    CONF_ENABLE_ALL,
    CONF_RAW_SENSORS,
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY,
    CONF_TRACKED_CLIENTS,
    DEFAULT_DETAILED_STATS,
    DEFAULT_ENABLE_ALL,
    DEFAULT_PORT,
    DEFAULT_RAW_SENSORS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .discovery import ScanResult, async_scan, narrow_network, scan_targets
from .parsers import normalize_mac, parse_show_interface, parse_show_version

_LOGGER = logging.getLogger(__name__)

_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        unit_of_measurement="s",
        mode=NumberSelectorMode.BOX,
    )
)


def _credentials_fields(defaults: Mapping[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
        vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_SSH_KEY, default=defaults.get(CONF_SSH_KEY, "")): str,
        vol.Required(
            CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        ): _INTERVAL_SELECTOR,
    }


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str, **_credentials_fields(defaults)}
    )


async def _probe(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Connect, read identity, disconnect. Raises AerohiveError subclasses."""
    client = await async_create_client(hass, data)
    try:
        version = parse_show_version(await client.run("show version"))
        interfaces = parse_show_interface(await client.run("show interface"))
    finally:
        await client.close()
    mgt = interfaces.get("mgt0")
    return {
        "mac": mgt.mac if mgt else None,
        "hostname": client.hostname,
        "platform": version.platform,
    }


class AerohiveConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        try:
            return await _probe(self.hass, data)
        except AerohiveAuthError:
            errors["base"] = "invalid_auth"
        except (OSError, asyncssh.KeyImportError):
            errors[CONF_SSH_KEY] = "invalid_key"
        except AerohiveError as err:
            _LOGGER.debug("Connection test failed: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during setup")
            errors["base"] = "unknown"
        return None

    def __init__(self) -> None:
        self._scan_task: asyncio.Task[list[ScanResult]] | None = None
        self._found: list[ScanResult] = []
        self._discovered_host: str | None = None

    def _defaults(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pre-fill the username (and port) from an existing entry."""
        defaults: dict[str, Any] = {}
        for entry in self._async_current_entries(include_ignore=False):
            defaults.setdefault(CONF_USERNAME, entry.data.get(CONF_USERNAME))
            defaults.setdefault(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))
        defaults.update({k: v for k, v in (extra or {}).items() if v not in (None, "")})
        return {k: v for k, v in defaults.items() if v is not None}

    def _configured_hosts(self) -> set[str]:
        return {e.data.get(CONF_HOST) for e in self._async_current_entries(include_ignore=False)}

    async def _async_finish(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Validate credentials and create the entry (None = show form again)."""
        data = {k: v for k, v in user_input.items() if v not in ("", None)}
        data[CONF_SCAN_INTERVAL] = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        if not data.get(CONF_PASSWORD) and not data.get(CONF_SSH_KEY):
            errors["base"] = "password_or_key"
            return None
        info = await self._validate(data, errors)
        if info is None:
            return None
        await self.async_set_unique_id(info["mac"] or data[CONF_HOST], raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
        title = info["hostname"] or info["platform"] or data[CONF_HOST]
        return self.async_create_entry(title=title, data=data)

    # ---------------------------------------------------------------- #
    # User: menu -> scan / manual
    # ---------------------------------------------------------------- #

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_finish(user_input, errors):
                return result
        return self.async_show_form(
            step_id="manual", data_schema=_user_schema(self._defaults(user_input)), errors=errors
        )

    async def _async_run_scan(self) -> list[ScanResult]:
        networks = []
        try:
            adapters = await network.async_get_adapters(self.hass)
        except Exception:  # noqa: BLE001 - network integration unavailable
            adapters = []
        for adapter in adapters:
            if not adapter.get("enabled"):
                continue
            for ipv4 in adapter.get("ipv4", []):
                try:
                    networks.append(narrow_network(ipv4["address"], int(ipv4["network_prefix"])))
                except (KeyError, ValueError):
                    continue
        own = {str(ipv4.get("address")) for a in adapters for ipv4 in a.get("ipv4", [])}
        targets = scan_targets(networks, exclude=own | self._configured_hosts())
        _LOGGER.debug("Scanning %d addresses for SSH", len(targets))
        return await async_scan(targets)

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(self._async_run_scan())
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scanning", progress_task=self._scan_task
            )
        try:
            self._found = self._scan_task.result()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Network scan failed")
            self._found = []
        self._scan_task = None
        return self.async_show_progress_done(next_step_id="pick" if self._found else "scan_empty")

    async def async_step_scan_empty(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="manual", data_schema=_user_schema(self._defaults()), errors={"base": "no_devices_found"}
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_finish(user_input, errors):
                return result
        likely = [r for r in self._found if r.likely_aerohive]
        options = [SelectOptionDict(value=r.host, label=r.label) for r in (likely or self._found)]
        if likely:
            others = [r for r in self._found if not r.likely_aerohive]
            options += [SelectOptionDict(value=r.host, label=f"{r.label} (other SSH device)") for r in others]
        default_host = (user_input or {}).get(CONF_HOST) or (options[0]["value"] if options else "")
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=default_host): SelectSelector(
                    SelectSelectorConfig(options=options, custom_value=True)
                ),
                **_credentials_fields(self._defaults(user_input)),
            }
        )
        return self.async_show_form(
            step_id="pick",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(likely)), "total": str(len(self._found))},
        )

    # ---------------------------------------------------------------- #
    # DHCP discovery
    # ---------------------------------------------------------------- #

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        mac = normalize_mac(discovery_info.macaddress)
        host = discovery_info.ip
        await self.async_set_unique_id(mac or host)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._async_abort_entries_match({CONF_HOST: host})
        self._discovered_host = host
        name = discovery_info.hostname or host
        self.context["title_placeholders"] = {"name": name, "host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovered_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_finish({CONF_HOST: self._discovered_host, **user_input}, errors):
                return result
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(_credentials_fields(self._defaults(user_input))),
            errors=errors,
            description_placeholders={
                "host": self._discovered_host,
                "name": self.context.get("title_placeholders", {}).get("name", self._discovered_host),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **{k: v for k, v in user_input.items() if v}}
            if await self._validate(data, errors) is not None:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> AerohiveOptionsFlow:  # type: ignore[no-untyped-def]
        return AerohiveOptionsFlow()


class AerohiveOptionsFlow(OptionsFlow):
    """Scan interval and opt-in client tracking."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current_tracked: list[str] = list(self.config_entry.options.get(CONF_TRACKED_CLIENTS, []))

        if user_input is not None:
            tracked: list[str] = []
            for raw in user_input.get(CONF_TRACKED_CLIENTS, []):
                mac = normalize_mac(raw.split(" ", 1)[0])
                if mac is None:
                    errors[CONF_TRACKED_CLIENTS] = "invalid_mac"
                    break
                if mac not in tracked:
                    tracked.append(mac)
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_TRACKED_CLIENTS: tracked,
                        CONF_DETAILED_STATS: user_input.get(CONF_DETAILED_STATS, DEFAULT_DETAILED_STATS),
                        CONF_RAW_SENSORS: user_input.get(CONF_RAW_SENSORS, DEFAULT_RAW_SENSORS),
                        CONF_ENABLE_ALL: user_input.get(CONF_ENABLE_ALL, DEFAULT_ENABLE_ALL),
                    }
                )

        options: list[SelectOptionDict] = []
        seen: set[str] = set()
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is not None and coordinator.data is not None:
            data = coordinator.data
            for mac, station in sorted(data.stations.items()):
                label = " ".join(
                    p for p in (mac, station.ip and f"({station.ip})", data.station_ssid(station)) if p
                )
                options.append(SelectOptionDict(value=mac, label=label))
                seen.add(mac)
        for mac in current_tracked:
            if mac not in seen:
                options.append(SelectOptionDict(value=mac, label=f"{mac} (not connected)"))

        interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=interval): _INTERVAL_SELECTOR,
                vol.Optional(CONF_TRACKED_CLIENTS, default=current_tracked): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True, custom_value=True)
                ),
                vol.Optional(
                    CONF_DETAILED_STATS,
                    default=self.config_entry.options.get(CONF_DETAILED_STATS, DEFAULT_DETAILED_STATS),
                ): bool,
                vol.Optional(
                    CONF_RAW_SENSORS,
                    default=self.config_entry.options.get(CONF_RAW_SENSORS, DEFAULT_RAW_SENSORS),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_ALL,
                    default=self.config_entry.options.get(CONF_ENABLE_ALL, DEFAULT_ENABLE_ALL),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
