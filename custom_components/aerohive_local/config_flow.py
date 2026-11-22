"""Config and options flow for Aerohive Local."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

import asyncssh
import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_VERIFY_SSL, CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
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
from .commands import DIALECT_EXOS, DIALECT_SWITCH
from .const import (
    CONF_API_SECRET,
    CONF_DETAILED_STATS,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_OPNSENSE,
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
from .discovery import (
    ScanResult,
    async_scan,
    is_aerohive_hostname,
    is_aerohive_mac,
    narrow_network,
    oui_vendor,
    scan_targets,
)
from .entity import radio_label
from . import openwrt as ow
from .parsers import (
    dialect_from_version,
    normalize_mac,
    parse_exos_show_switch,
    parse_show_interface,
    parse_show_version,
)

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


def _short_model(neighbor: Any) -> str | None:
    """A short model name from an LLDP neighbour ('Aerohive SR2208P', 'EC-200', 'ExtremeXOS 12.6.3.4')."""
    raw = getattr(neighbor, "raw", {}) or {}
    if raw.get("med_model_name"):
        return " ".join(v for v in (raw.get("med_manufacturer_name"), raw["med_model_name"]) if v)
    text = (getattr(neighbor, "system_description", None) or "").strip()
    if not text:
        return None
    if m := re.search(r"ExtremeXOS(?: \(\S+\))? version (\S+)", text):
        return f"ExtremeXOS {m.group(1)}"
    return re.split(r"[,:;]", text)[0][:40].strip() or None


def network_knowledge(hass: HomeAssistant) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """What the devices already set up know about others: (by IP, by MAC) -> name/model/seen_on.

    From LLDP neighbours, switch MAC tables and access point client lists.
    """
    from .coordinator import AerohiveCoordinator  # noqa: PLC0415 - avoid an import cycle

    by_ip: dict[str, dict[str, str]] = {}
    by_mac: dict[str, dict[str, str]] = {}

    def add(target: dict[str, dict[str, str]], key: str | None, **info: str | None) -> None:
        if not key:
            return
        entry = target.setdefault(key, {})
        for k, v in info.items():
            if v and k not in entry:
                entry[k] = v

    for config_entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(config_entry, "runtime_data", None)
        if not isinstance(coordinator, AerohiveCoordinator) or coordinator.data is None:
            continue
        data = coordinator.data
        device = data.hostname or config_entry.title
        for port, neighbor in data.lldp.items():
            info = {"name": neighbor.system_name, "model": _short_model(neighbor), "seen_on": f"{device} port {port}"}
            add(by_ip, neighbor.management_address, **info)
            add(by_mac, normalize_mac(neighbor.chassis_id), **info)
        uplinks = data.uplink_ports()
        for fdb in data.fdb:
            if fdb.port and fdb.port not in uplinks:
                add(by_mac, fdb.mac, seen_on=f"{device} port {fdb.port}")
        for station in data.stations.values():
            ssid = data.station_ssid(station)
            add(by_mac, station.mac, name=station.hostname, seen_on=f"{device} Wi-Fi" + (f" ({ssid})" if ssid else ""))
            add(by_ip, station.ip, name=station.hostname, seen_on=f"{device} Wi-Fi" + (f" ({ssid})" if ssid else ""))
    return by_ip, by_mac


def _details_text(**details: str | None) -> str:
    """Markdown lines for the discovery form ('- **MAC address:** ...')."""
    labels = {"model": "Model", "mac": "MAC address", "vendor": "Made by", "hostname": "Hostname", "seen_on": "Connected to"}
    lines = [f"- **{labels[k]}:** {v}" for k, v in details.items() if v and k in labels]
    return "\n".join(lines)


async def _is_openwrt(client: Any) -> bool:
    """Connect (the prompt tells the CLI) and say whether it's an OpenWrt shell."""
    await client.connect()
    return client.dialect == ow.DIALECT_OPENWRT


async def _probe_opnsense(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Read an OPNsense router's identity through its API. Raises AerohiveError subclasses."""
    from . import opnsense as opn  # noqa: PLC0415

    client = await async_create_client(hass, data)
    info = opn.parse_system_information(await client.get(opn.SYSTEM_INFORMATION))  # type: ignore[attr-defined]
    if info.hostname is None and info.version is None:
        raise AerohiveError("not an OPNsense API")
    ports = opn.parse_interfaces(await client.get(opn.INTERFACES))  # type: ignore[attr-defined]
    lan = next((p for p in ports.values() if p.identifier == "lan"), None) or next(iter(ports.values()), None)
    return {"mac": normalize_mac(lan.mac) if lan else None, "hostname": info.hostname, "platform": "OPNsense"}


async def _probe(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Connect, read identity, disconnect. Raises AerohiveError subclasses."""
    if data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_OPNSENSE:
        return await _probe_opnsense(hass, data)
    client = await async_create_client(hass, data)
    try:
        if client.dialect == ow.DIALECT_OPENWRT or (not client.connected and await _is_openwrt(client)):
            board = ow.parse_board(await client.run(ow.BOARD))
            mac = normalize_mac(ow.bridge_mac(await client.run(ow.NETWORK_DEVICES)))
            return {"mac": mac, "hostname": board.hostname or client.hostname, "platform": board.model}
        version_out = await client.run("show version")
        if (detected := dialect_from_version(version_out)) and detected != client.dialect:
            await client.async_set_dialect(detected)
        version = parse_show_version(version_out)
        mac, platform = version.mac, version.platform
        if client.dialect == DIALECT_EXOS:
            info = parse_exos_show_switch(await client.run(commands.EXOS_SHOW_SWITCH))
            mac, platform = info.mac, info.system_type
        elif client.dialect != DIALECT_SWITCH:
            interfaces = parse_show_interface(await client.run("show interface"))
            mgt = interfaces.get("mgt0")
            mac = (mgt.mac if mgt else None) or mac
    finally:
        await client.close()
    return {"mac": mac, "hostname": client.hostname, "platform": platform}


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
        self._details: str | None = None

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
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual", "opnsense"])

    async def async_step_opnsense(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """An OPNsense router, through its REST API (API key and secret)."""
        errors: dict[str, str] = {}
        values = dict(user_input or {})
        if user_input is not None:
            data = {
                CONF_DEVICE_TYPE: DEVICE_TYPE_OPNSENSE,
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input.get(CONF_PORT, 443)),
                CONF_API_KEY: user_input[CONF_API_KEY].strip(),
                CONF_API_SECRET: user_input[CONF_API_SECRET].strip(),
                CONF_VERIFY_SSL: bool(user_input.get(CONF_VERIFY_SSL, False)),
                CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            }
            info = await self._validate(data, errors)
            if info is not None:
                await self.async_set_unique_id(info["mac"] or f"opnsense-{data[CONF_HOST]}", raise_on_progress=False)
                self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
                return self.async_create_entry(title=info["hostname"] or data[CONF_HOST], data=data)
        return self.async_show_form(
            step_id="opnsense",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=values.get(CONF_HOST, "")): str,
                    vol.Optional(CONF_PORT, default=values.get(CONF_PORT, 443)): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                    vol.Required(CONF_API_KEY, default=values.get(CONF_API_KEY, "")): str,
                    vol.Required(CONF_API_SECRET): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    vol.Optional(CONF_VERIFY_SSL, default=values.get(CONF_VERIFY_SSL, False)): bool,
                    vol.Optional(CONF_SCAN_INTERVAL, default=values.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                }
            ),
            errors=errors,
        )

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
        by_ip, by_mac = network_knowledge(self.hass)
        configured = {e.data.get(CONF_HOST) for e in self._async_current_entries(include_ignore=False)}
        for result in self._found:
            known = {**by_mac.get(normalize_mac(result.mac) or "", {}), **by_ip.get(result.host, {})}
            result.name = result.name or known.get("name")
            result.model = result.model or known.get("model")
            result.seen_on = result.seen_on or known.get("seen_on")
            result.configured = result.host in configured
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
        # A device already set up: just follow its new IP address.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._async_abort_entries_match({CONF_HOST: host})
        # DHCP also reports every device linked to this integration, such as phones and
        # laptops with a device tracker. Only offer what looks like Aerohive / Extreme gear.
        if not (is_aerohive_mac(mac) or is_aerohive_hostname(discovery_info.hostname)):
            return self.async_abort(reason="not_aerohive_device")
        self._discovered_host = host
        name = discovery_info.hostname or host
        self.context["title_placeholders"] = {"name": name, "host": host}
        known = network_knowledge(self.hass)[1].get(mac or "", {})
        self._details = _details_text(
            model=known.get("model"), mac=mac, vendor=oui_vendor(mac), hostname=discovery_info.hostname,
            seen_on=known.get("seen_on"),
        )
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
        self._details = _details_text(
            model=discovery_info.get("model"), mac=mac, vendor=oui_vendor(mac), seen_on=discovery_info.get("seen_on"),
        )
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
                "details": self._details or "",
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
        if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_OPNSENSE:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_API_KEY, default=entry.data.get(CONF_API_KEY, "")): str,
                        vol.Required(CONF_API_SECRET): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    }
                ),
                errors=errors,
            )
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
        if self._is_overview:
            return target.data.ssid_names()
        return sorted(s for s in target.data.ssids if target.data.ssid_configurable(s))

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
