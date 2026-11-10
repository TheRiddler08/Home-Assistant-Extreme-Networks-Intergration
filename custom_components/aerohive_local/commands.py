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
# Tried in order until one is accepted: HiveOS, then FASTPATH-style switch CLI.
DISABLE_PAGING_COMMANDS = ("console page 0", "terminal length 0")

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


# --------------------------------------------------------------------------
# Switch ports / PoE (SR-series switches, BR-series Ethernet ports)
# --------------------------------------------------------------------------

_PORT_CMD_RE = re.compile(r"^eth\d+(?:/\d+){0,2}$")

# Assumptions (unverified): HiveOS switch CLI uses
#   "interface <port> shutdown" / "no interface <port> shutdown"
#   "interface <port> pse shutdown" / "no interface <port> pse shutdown"
POE_STATUS_COMMANDS = ("show pse", "show poe", "show interface pse")
MAC_TABLE_COMMANDS = ("show mac-address-table", "show mac-address-table all", "show fdb")
LLDP_COMMANDS = ("show lldp neighbor", "show lldp neighbors")


def validate_port(port: str) -> str:
    port = (port or "").strip().lower()
    if not _PORT_CMD_RE.match(port):
        raise InvalidArgument(f"Invalid Ethernet port: {port!r}")
    return port


def port_enable(port: str, enable: bool) -> str:
    cmd = f"interface {validate_port(port)} shutdown"
    return f"no {cmd}" if enable else cmd


def port_poe(port: str, enable: bool) -> str:
    cmd = f"interface {validate_port(port)} pse shutdown"
    return f"no {cmd}" if enable else cmd


# --------------------------------------------------------------------------
# SSID lifecycle
# --------------------------------------------------------------------------

_SSID_NAME_RE = re.compile(r"^[\x20-\x21\x23-\x7e]{1,32}$")


def validate_ssid_name(name: str) -> str:
    if not _SSID_NAME_RE.match(name or "") or name != name.strip():
        raise InvalidArgument("SSID name must be 1-32 printable ASCII characters without double quotes")
    return name


def ssid_hide(ssid: str, hidden: bool) -> str:
    cmd = f"ssid {quote(ssid, 'SSID')} hide-ssid"
    return cmd if hidden else f"no {cmd}"


def create_ssid(ssid: str, radios: list[str], password: str | None) -> list[str]:
    """Create a security-object + SSID with the same name and bind it to radios.

    Assumption (unverified): mirrors how HiveManager provisions an SSID.
    Without a password the security-object stays open.
    """
    validate_ssid_name(ssid)
    name = quote(ssid, "SSID")
    cmds = [f"security-object {name}"]
    if password:
        cmds.append(ssid_psk(ssid, password))
    cmds += [f"ssid {name}", f"ssid {name} security-object {name}"]
    cmds += [ssid_bind(radio, ssid, True) for radio in radios]
    return cmds


def delete_ssid(ssid: str, bound_radios: list[str], security_object: str | None) -> list[str]:
    """Unbind from radios, remove the SSID and (if it has its own) its security-object."""
    name = quote(ssid, "SSID")
    cmds = [ssid_bind(radio, ssid, False) for radio in bound_radios]
    cmds.append(f"no ssid {name}")
    if security_object and security_object == ssid:
        cmds.append(f"no security-object {quote(security_object, 'security object')}")
    return cmds


# --------------------------------------------------------------------------
# SR-series switch CLI (FASTPATH-style, prompt "(AH-Switch) #")
# --------------------------------------------------------------------------
# Verified against an SR2208P (software 1.0.1.28): show version, show network,
# show port all, show poe, show mac-addr-table, show lldp remote-device ...
# Assumptions (unverified): per-port PoE status commands, config-mode
# commands below and "write memory".

DIALECT_HIVEOS = "hiveos"
DIALECT_SWITCH = "switch"

SW_SHOW_VERSION = "show version"
SW_SHOW_NETWORK = "show network"
SW_SHOW_PORTS = "show port all"
SW_SHOW_POE = "show poe"
# "show poe port all" is rejected on 1.0.1.28 ("port" is accepted, "all" is not).
SW_POE_PORT_COMMANDS = (
    "show poe port info all",
    "show poe port info",
    "show poe port configuration all",
    "show poe port status all",
)
# Fallback: one port at a time.
# Verified on SR2208P 1.0.1.28: "show poe port info 1/0/1" (one-row table).
SW_POE_PORT_SINGLE = ("show poe port info {port}", "show poe port {port}")
SW_MAC_TABLE = "show mac-addr-table"
SW_LLDP = "show lldp remote-device all"
SW_LLDP_DETAIL = "show lldp remote-device detail {port}"
SW_PORT_DETAIL = "show interface ethernet {port}"
SW_SAVE = "write memory"
SW_REBOOT = "reload"

_USP_CMD_RE = re.compile(r"^[1-9]\d*/\d+/\d+$")


def validate_switch_port(port: str) -> str:
    port = (port or "").strip()
    if not _USP_CMD_RE.match(port):
        raise InvalidArgument(f"Invalid switch port: {port!r} (expected unit/slot/port, e.g. 1/0/3)")
    return port


def switch_interface_block(port: str, lines: list[str]) -> list[str]:
    """Enter config mode for one interface, run lines, and return to privileged mode."""
    return ["configure", f"interface {validate_switch_port(port)}", *lines, "exit", "exit"]


def switch_port_enable(port: str, enable: bool) -> list[str]:
    return switch_interface_block(port, ["no shutdown" if enable else "shutdown"])


def switch_poe(port: str, enable: bool) -> list[str]:
    return switch_interface_block(port, ["poe" if enable else "no poe"])
