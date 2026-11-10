"""Config and options flow for Aerohive Local."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import asyncssh
import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from . import async_create_client, commands
from .client import AerohiveAuthError, AerohiveError
from .commands import DIALECT_SWITCH
from .const import (
    CONF_DETAILED_STATS,
    CONF_ENABLE_ALL,
    CONF_ENTRY_TYPE,
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
    ENTRY_TYPE_OVERVIEW,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .discovery import ScanResult, async_scan, narrow_network, scan_targets
from .entity import radio_label
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
        mac = version.mac
        if client.dialect != DIALECT_SWITCH:
            interfaces = parse_show_interface(await client.run("show interface"))
            mgt = interfaces.get("mgt0")
            mac = (mgt.mac if mgt else None) or mac
    finally:
        await client.close()
    return {"mac": mac, "hostname": client.hostname, "platform": version.platform}


class AerohiveConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 3

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
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_OVERVIEW:
                continue
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

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """A configured device saw this AP/switch as an LLDP neighbour."""
        host = discovery_info[CONF_HOST]
        mac = normalize_mac(discovery_info.get("mac"))
        await self.async_set_unique_id(mac or host)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._async_abort_entries_match({CONF_HOST: host})
        self._discovered_host = host
        self.context["title_placeholders"] = {"name": discovery_info.get("name") or host, "host": host}
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

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the network overview entry (started automatically)."""
        if import_data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_OVERVIEW:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id("overview")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Aerohive Network", data={CONF_ENTRY_TYPE: ENTRY_TYPE_OVERVIEW})

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


WIFI_ADD = "__add__"
WIFI_DONE = "__done__"
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class AerohiveOptionsFlow(OptionsFlow):
    """Settings, plus the Wi-Fi manager (list / add / edit / delete SSIDs)."""

    def __init__(self) -> None:
        self._ssid: str | None = None
        self._message = ""

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    @property
    def _is_overview(self) -> bool:
        return self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_OVERVIEW

    @property
    def _target(self) -> Any:
        return getattr(self.config_entry, "runtime_data", None)

    def _ssid_names(self) -> list[str]:
        target = self._target
        if target is None or target.data is None:
            return []
        return target.data.ssid_names() if self._is_overview else sorted(target.data.ssids)

    def _ssid_label(self, ssid: str) -> str:
        data = self._target.data
        if self._is_overview:
            aps = data.aps_with_ssid(ssid)
            on = [ap for ap in aps if ap.data.ssid_broadcasting(ssid)]
            clients = sum(ap.data.clients_on_ssid(ssid) for ap in aps)
            hidden = any(ap.data.ssid_hidden(ssid) for ap in aps)
            state = f"broadcasting on {len(on)} of {len(aps)} APs" if on else f"off on all {len(aps)} APs"
        else:
            radios = [r for r in data.radio_names() if data.ssid_on_radio(ssid, r)]
            bands = [radio_label(data, r) for r in radios]
            clients = data.clients_on_ssid(ssid)
            hidden = bool(data.ssid_hidden(ssid))
            state = f"on {', '.join(bands)}" if bands else "not broadcasting"
        parts = [state, f"{clients} client{'s' if clients != 1 else ''}"]
        if hidden:
            parts.append("hidden")
        return f"{ssid} — {' · '.join(parts)}"

    def _radio_options(self) -> list[SelectOptionDict]:
        data = self._target.data
        return [SelectOptionDict(value=r, label=radio_label(data, r)) for r in data.radio_names()]

    def _device_error(self, err: Exception, errors: dict[str, str], placeholders: dict[str, str]) -> None:
        errors["base"] = "device_error"
        placeholders["error"] = str(err)

    # ---------------------------------------------------------------- #
    # Entry point
    # ---------------------------------------------------------------- #

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._is_overview:
            return await self.async_step_wifi()
        target = self._target
        if target is not None and target.data is not None and target.data.radio_names():
            return self.async_show_menu(step_id="init", menu_options=["wifi", "settings"])
        return await self.async_step_settings()

    # ---------------------------------------------------------------- #
    # Wi-Fi manager
    # ---------------------------------------------------------------- #

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        target = self._target
        if target is None or target.data is None:
            return self.async_abort(reason="device_unavailable")
        if user_input is not None:
            choice = user_input["network"]
            if choice == WIFI_DONE:
                return self.async_create_entry(data=dict(self.config_entry.options))
            self._message = ""
            if choice == WIFI_ADD:
                return await self.async_step_wifi_add()
            self._ssid = choice
            return await self.async_step_wifi_edit()

        options = [SelectOptionDict(value=s, label=self._ssid_label(s)) for s in self._ssid_names()]
        options.append(SelectOptionDict(value=WIFI_ADD, label="➕ Add a new Wi-Fi network"))
        options.append(SelectOptionDict(value=WIFI_DONE, label="✔ Done"))
        schema = vol.Schema(
            {
                vol.Required("network"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(
            step_id="wifi",
            data_schema=schema,
            description_placeholders={"message": self._message, "count": str(len(self._ssid_names()))},
        )

    async def async_step_wifi_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        target, ssid = self._target, self._ssid
        if target is None or target.data is None or ssid not in self._ssid_names():
            self._message = f"{ssid} no longer exists."
            return await self.async_step_wifi()
        data = target.data
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"ssid": ssid, "message": ""}

        if user_input is not None:
            if user_input.get("delete"):
                return await self.async_step_wifi_delete()
            password = user_input.get(CONF_PASSWORD) or None
            if password:
                try:
                    commands.validate_psk(password)
                except commands.InvalidArgument:
                    errors[CONF_PASSWORD] = "invalid_password"
            if not errors:
                try:
                    if self._is_overview:
                        await target.async_update_ssid(
                            ssid, password=password, enabled=user_input.get("enabled"), hidden=user_input.get("hidden")
                        )
                        changed = True
                    else:
                        max_clients = user_input.get("max_clients")
                        changed = await target.async_update_ssid(
                            ssid,
                            password=password,
                            radios=list(user_input.get("radios", [])),
                            hidden=user_input.get("hidden"),
                            max_clients=int(max_clients) if max_clients is not None else None,
                        )
                except HomeAssistantError as err:
                    self._device_error(err, errors, placeholders)
                else:
                    self._message = f"✔ Saved changes to {ssid}." if changed else f"No changes made to {ssid}."
                    return await self.async_step_wifi()

        fields: dict[Any, Any] = {vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR}
        if self._is_overview:
            aps = data.aps_with_ssid(ssid)
            fields[vol.Required("enabled", default=all(ap.data.ssid_enabled(ssid) for ap in aps))] = bool
            if any(ap.data.config is not None for ap in aps):
                fields[vol.Required("hidden", default=any(ap.data.ssid_hidden(ssid) for ap in aps))] = bool
            placeholders["message"] = "Access points with this network: " + ", ".join(ap.title for ap in aps)
        else:
            bound = [r for r in data.radio_names() if data.ssid_on_radio(ssid, r)]
            fields[vol.Optional("radios", default=bound)] = SelectSelector(
                SelectSelectorConfig(options=self._radio_options(), multiple=True, mode=SelectSelectorMode.LIST)
            )
            if data.config is not None:
                fields[vol.Required("hidden", default=bool(data.ssid_hidden(ssid)))] = bool
            current_max = data.ssids[ssid].max_client
            if current_max is not None:
                fields[vol.Required("max_clients", default=current_max)] = NumberSelector(
                    NumberSelectorConfig(min=1, max=255, step=1, mode=NumberSelectorMode.BOX)
                )
            security = data.config.ssid_security(ssid) if data.config else None
            if security:
                placeholders["message"] = f"Security: {security}"
        fields[vol.Optional("delete", default=False)] = bool
        return self.async_show_form(
            step_id="wifi_edit", data_schema=vol.Schema(fields), errors=errors, description_placeholders=placeholders
        )

    async def async_step_wifi_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        ssid = self._ssid
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"ssid": ssid or ""}
        if user_input is not None:
            if not user_input.get("confirm"):
                return await self.async_step_wifi_edit()
            try:
                await self._target.async_delete_ssid(ssid)
            except HomeAssistantError as err:
                self._device_error(err, errors, placeholders)
            else:
                self._message = f"✔ Deleted {ssid}."
                self._ssid = None
                return await self.async_step_wifi()
        return self.async_show_form(
            step_id="wifi_delete",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_wifi_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        target = self._target
        if target is None or target.data is None:
            return self.async_abort(reason="device_unavailable")
        data = target.data
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"message": ""}
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            password = user_input.get(CONF_PASSWORD) or None
            try:
                commands.validate_ssid_name(name)
            except commands.InvalidArgument:
                errors["name"] = "invalid_ssid_name"
            if name in self._ssid_names():
                errors["name"] = "ssid_exists"
            if password:
                try:
                    commands.validate_psk(password)
                except commands.InvalidArgument:
                    errors[CONF_PASSWORD] = "invalid_password"
            if not errors:
                try:
                    if self._is_overview:
                        await target.async_create_ssid(
                            name, password, None, bool(user_input.get("hidden")),
                            access_points=list(user_input.get("access_points") or []) or None,
                        )  # fmt: skip
                    else:
                        await target.async_create_ssid(
                            name, password, list(user_input.get("radios", [])), bool(user_input.get("hidden"))
                        )
                except HomeAssistantError as err:
                    self._device_error(err, errors, placeholders)
                else:
                    self._message = f"✔ Created {name}" + ("" if password else " (open network, no password)") + "."
                    return await self.async_step_wifi()

        defaults = user_input or {}
        fields: dict[Any, Any] = {
            vol.Required("name", default=defaults.get("name", "")): str,
            vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
        }
        if self._is_overview:
            titles = [ap.title for ap in data.aps]
            fields[vol.Optional("access_points", default=defaults.get("access_points", titles))] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=t, label=t) for t in titles], multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )  # fmt: skip
        else:
            fields[vol.Optional("radios", default=defaults.get("radios", data.radio_names()))] = SelectSelector(
                SelectSelectorConfig(options=self._radio_options(), multiple=True, mode=SelectSelectorMode.LIST)
            )
        fields[vol.Optional("hidden", default=defaults.get("hidden", False))] = bool
        return self.async_show_form(
            step_id="wifi_add", data_schema=vol.Schema(fields), errors=errors, description_placeholders=placeholders
        )

    # ---------------------------------------------------------------- #
    # Settings (scan interval, tracked clients, entity options)
    # ---------------------------------------------------------------- #

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Scan interval, client tracking and entity options."""
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
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)
