"""Constants for the Aerohive Local integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "aerohive_local"
MANUFACTURER: Final = "Extreme Networks / Aerohive"

CONF_SSH_KEY: Final = "ssh_key_path"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_TRACKED_CLIENTS: Final = "tracked_clients"

DEFAULT_PORT: Final = 22
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600

# Control actions requested within this window are coalesced; only the last
# requested value is sent to the device.
CONTROL_DEBOUNCE_SECONDS: Final = 1.5
# Boot parameters rarely change; re-read them at most this often.
BOOT_PARAM_REFRESH_SECONDS: Final = 6 * 3600

SERVICE_RUN_SHOW_COMMAND: Final = "run_show_command"
SERVICE_SET_SSID_PASSWORD: Final = "set_ssid_password"
SERVICE_DISCONNECT_CLIENT: Final = "disconnect_client"

ATTR_DEVICE_ID: Final = "device_id"
ATTR_COMMAND: Final = "command"
ATTR_REDACT: Final = "redact"
ATTR_SSID: Final = "ssid"
ATTR_PASSWORD: Final = "password"
ATTR_SECURITY_OBJECT: Final = "security_object"
ATTR_MAC: Final = "mac"
