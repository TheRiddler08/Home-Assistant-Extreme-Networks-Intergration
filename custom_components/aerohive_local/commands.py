"""Pure builders/validators for Aerohive CLI configuration commands.

No Home Assistant imports. Every user-supplied value is validated so that it
cannot inject extra CLI input (newlines, control characters, quotes).
"""

from __future__ import annotations

import re

from .parsers import to_aerohive_mac

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_RADIO_RE = re.compile(r"^wifi\d+$")
_SHOW_RE = re.compile(r"^show(\s+[A-Za-z0-9_\-.:/\"| ]+)?$", re.IGNORECASE)
# HiveOS PSK: 8-63 printable ASCII. Double quotes are refused because the CLI
# has no escape mechanism for them inside a quoted argument.
_PSK_RE = re.compile(r'^[\x20\x21\x23-\x7e]{8,63}$')
_SECRET_ARG_RE = re.compile(r"(?i)\b(ascii-key|hex-key|password|passphrase)\s+(\"[^\"]*\"|\S+)")

SAVE_CONFIG = "save config"
REBOOT = "reboot"
DISABLE_PAGING = "console page 0"

# Assumption (unverified): HiveOS / IQ Engine de-authenticates a client with
# "clear auth station mac <mac>". The fallback form is tried if the device
# rejects the first one.
DISCONNECT_TEMPLATES = (
    "clear auth station mac {mac}",
    "clear auth station {mac}",
)


class InvalidArgument(ValueError):
    """Raised when a value is not safe to send to the CLI."""


def quote(value: str, what: str = "value") -> str:
    """Return a CLI-safe (quoted if needed) argument."""
    if not value or not value.strip():
        raise InvalidArgument(f"{what} must not be empty")
    if _CONTROL_RE.search(value):
        raise InvalidArgument(f"{what} contains control characters")
    if '"' in value:
        raise InvalidArgument(f"{what} must not contain double quotes")
    if value != value.strip() or re.search(r"\s", value):
        return f'"{value}"'
    return value


def validate_radio(radio: str) -> str:
    radio = radio.strip().lower()
    if not _RADIO_RE.match(radio):
        raise InvalidArgument(f"Invalid radio interface: {radio!r}")
    return radio


def ssid_bind(radio: str, ssid: str, enable: bool) -> str:
    """'interface wifi0 ssid X' / 'no interface wifi0 ssid X'."""
    cmd = f"interface {validate_radio(radio)} ssid {quote(ssid, 'SSID')}"
    return cmd if enable else f"no {cmd}"


def ssid_max_client(ssid: str, value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArgument("max-client must be an integer")
    if not 1 <= value <= 255:
        raise InvalidArgument("max-client must be between 1 and 255")
    return f"ssid {quote(ssid, 'SSID')} max-client {value}"


def validate_psk(key: str) -> str:
    if not _PSK_RE.match(key or ""):
        raise InvalidArgument(
            "Wi-Fi key must be 8-63 printable ASCII characters without double quotes"
        )
    return key


def ssid_psk(security_object: str, key: str) -> str:
    validate_psk(key)
    return (
        f"security-object {quote(security_object, 'security object')} "
        f"security protocol-suite wpa2-aes-psk ascii-key {quote(key, 'key')}"
    )


def disconnect_commands(mac: str) -> list[str]:
    try:
        aero = to_aerohive_mac(mac)
    except ValueError as err:
        raise InvalidArgument(str(err)) from err
    return [t.format(mac=aero) for t in DISCONNECT_TEMPLATES]


def validate_show_command(command: str) -> str:
    """Allow only single-line read-only 'show ...' commands."""
    command = (command or "").strip()
    if _CONTROL_RE.search(command):
        raise InvalidArgument("Command must be a single line")
    if not _SHOW_RE.match(command):
        raise InvalidArgument(
            "Only 'show ...' commands made of letters, digits, spaces and -_.:/\"| are allowed"
        )
    return command


def redact(text: str) -> str:
    """Hide secrets that may appear in a command line or config output."""
    return _SECRET_ARG_RE.sub(lambda m: f"{m.group(1)} ***", text or "")
