"""Constants for the Aerohive Local integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "aerohive_local"
MANUFACTURER: Final = "Extreme Networks / Aerohive"

CONF_SSH_KEY: Final = "ssh_key_path"
CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_OVERVIEW: Final = "overview"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_TRACKED_CLIENTS: Final = "tracked_clients"
CONF_DETAILED_STATS: Final = "detailed_stats"
CONF_RAW_SENSORS: Final = "raw_sensors"
CONF_ENABLE_ALL: Final = "enable_all_entities"

DEFAULT_DETAILED_STATS: Final = True
DEFAULT_RAW_SENSORS: Final = True
DEFAULT_ENABLE_ALL: Final = False

DEFAULT_PORT: Final = 22
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600

# Control actions requested within this window are coalesced; only the last
# requested value is sent to the device.
CONTROL_DEBOUNCE_SECONDS: Final = 1.5
# Mostly static data (boot-param, hw-info, mgt0 address) is re-read this often.
SLOW_REFRESH_SECONDS: Final = 900

SERVICE_RUN_SHOW_COMMAND: Final = "run_show_command"
SERVICE_SET_SSID_PASSWORD: Final = "set_ssid_password"
SERVICE_DISCONNECT_CLIENT: Final = "disconnect_client"
SERVICE_CREATE_SSID: Final = "create_ssid"
SERVICE_DELETE_SSID: Final = "delete_ssid"
SERVICE_POWER_CYCLE_POE: Final = "power_cycle_poe"
ATTR_RADIOS: Final = "radios"
ATTR_PORT: Final = "port"

ATTR_DEVICE_ID: Final = "device_id"
ATTR_COMMAND: Final = "command"
ATTR_REDACT: Final = "redact"
ATTR_SSID: Final = "ssid"
ATTR_PASSWORD: Final = "password"
ATTR_SECURITY_OBJECT: Final = "security_object"
ATTR_MAC: Final = "mac"
