"""Pure parsing helpers for Aerohive HiveOS / Extreme IQ Engine CLI output.

This module must stay free of Home Assistant (and asyncssh) imports so it can
be unit tested in isolation. Every public parser is defensive: unknown or
malformed lines are skipped, never raised on.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Generic text cleanup
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
# "--More--", "-- More --", " --More-- " optionally followed by the carriage
# return / space sequence many CLIs use to wipe the pager marker.
# Also matches the FASTPATH-style "--More-- or (q)uit" used by SR-series switches.
MORE_RE = re.compile(
    r"[ \t]*-+[ \t]*More[ \t]*-+(?:[ \t]*or[ \t]*\(q\)uit)?[ \t]*(?:\r[ \t]*)*", re.IGNORECASE
)
MORE_TAIL_RE = re.compile(
    r"(?:-+[ \t]*More[ \t]*-+(?:[ \t]*or[ \t]*\(q\)uit)?|Press <SPACE> to continue or <Q> to quit:)[ \t]*$",
    re.IGNORECASE,
)
_EXOS_MORE_RE = re.compile(r"Press <SPACE> to continue or <Q> to quit:[ \t]*(?:\r[ \t]*)*", re.IGNORECASE)
CONFIRM_TAIL_RE = re.compile(
    r"(?:(\((?:y/n|yes/no)\)|\[(?:y/n|yes/no)\]|<(?:y/n|yes/no)>|\b(?:y/n|yes/no)\b)[^\n]{0,20}[:?]?"
    # ExtremeXOS reboot: "(y - save and reboot, n - reboot without save, <cr> - cancel command)"
    r"|\(y\s*-[^)\n]*\bn\s*-[^)\n]*\))\s*$",
    re.IGNORECASE,
)
# The ExtremeXOS three-way reboot prompt ("y - save and reboot, n - reboot without save").
SAVE_CHOICE_RE = re.compile(r"\(y\s*-\s*save[^)\n]*\bn\s*-", re.IGNORECASE)
# CLI prompt, anchored at the end of the buffer:
#   HiveOS / IQ Engine APs and routers: "AH-7c0680#"
#   SR-series switches (FASTPATH-style): "(AH-Switch) #", "(AH-Switch) >",
#   and in config mode "(AH-Switch) (Config)#" / "(AH-Switch) (Interface 1/0/1)#"
#   ExtremeXOS switches: "X450e-48p.3 #", "* X450e-48p.4 #" (* = unsaved changes),
#   stacks "* Slot-1 Stack.2 #"
PROMPT_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:"
    r"(?P<unsaved>\*[ \t]+)?(?:(?P<slot>Slot-\d+)[ \t]+)?(?P<xhost>[A-Za-z0-9][A-Za-z0-9_\-\.]*?)\.(?P<seq>\d+)[ \t]+"
    r"|\((?P<phost>[^()\n]{1,64})\)(?:[ \t]*\((?P<submode>[^()\n]{1,64})\))?[ \t]*"
    r"|(?P<host>[A-Za-z0-9][A-Za-z0-9_\-\.]*))"
    r"(?P<mode>[#>])[ \t]*$"
)


def prompt_host(match: re.Match[str]) -> str:
    """Hostname from a PROMPT_RE match (any prompt style)."""
    return match.group("host") or match.group("phost") or match.group("xhost")

NONE_VALUES = {"", "-", "--", "n/a", "na", "none"}


def clean_output(text: str) -> str:
    """Strip ANSI codes, pager markers, backspaces and normalise newlines."""
    if not text:
        return ""
    text = ANSI_RE.sub("", text)
    text = MORE_RE.sub("", text)
    text = _EXOS_MORE_RE.sub("", text)
    if "\b" in text:
        out: list[str] = []
        for char in text:
            if char == "\b":
                if out and out[-1] != "\n":
                    out.pop()
            else:
                out.append(char)
        text = "".join(out)
    text = text.replace("\r\n", "\n")
    # A lone CR means "return to column 0"; keep only what follows it on
    # that line unless it is just whitespace overwrite noise.
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p.strip()]
            line = parts[-1] if parts else ""
        lines.append(line.rstrip())
    return "\n".join(lines)


def strip_echo_and_prompt(output: str, command: str) -> str:
    """Remove the echoed command (first line) and trailing prompt."""
    lines = output.split("\n")
    # Drop leading blank lines and the echo line.
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and command.strip() and command.strip() in lines[0]:
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and PROMPT_RE.search("\n" + lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip("\n")


# Lines that indicate the device rejected a command.
_ERROR_PATTERNS = [
    re.compile(r"^\s*ERROR\b.*$", re.MULTILINE),
    re.compile(r"^\s*Error\s*:.*$", re.MULTILINE | re.IGNORECASE),  # ExtremeXOS: "Error: Port 53 is invalid"
    re.compile(r"^.*\b[Uu]nknown keyword\b.*$", re.MULTILINE),
    re.compile(r"^.*\bInvalid\b.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^.*\bIncomplete command\b.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^.*\bCommand not found\b.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^.*\bAmbiguous command\b.*$", re.MULTILINE | re.IGNORECASE),
]


def find_cli_error(output: str) -> str | None:
    """Return the device's error message if the output reports a failure."""
    if not output:
        return None
    for pattern in _ERROR_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(0).strip()
    return None


_UNSUPPORTED_RE = re.compile(
    r"unknown keyword|invalid input|invalid interface|invalid command|incomplete command|command not found|"
    r"ambiguous command|^\s*ERROR\b|^\s*%",
    re.IGNORECASE,
)


def is_unsupported_command(output: str) -> bool:
    """True if a read-only command was rejected by the CLI.

    Only the first few non-empty lines are checked, so counters such as
    "Rx invalid frames=0" in a long detail output do not trigger it.
    """
    lines = [line for line in (output or "").split("\n") if line.strip()][:3]
    return any(_UNSUPPORTED_RE.search(line) for line in lines)


# --------------------------------------------------------------------------
# Small value helpers
# --------------------------------------------------------------------------

MAC_HEX_RE = re.compile(
    r"^(?:[0-9a-f]{4}[:.\-]){2}[0-9a-f]{4}$"
    r"|^(?:[0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$"
    r"|^[0-9a-f]{12}$",
    re.IGNORECASE,
)
MAC_SEARCH_RE = re.compile(
    r"\b(?:[0-9a-f]{4}[:.]){2}[0-9a-f]{4}\b|\b(?:[0-9a-f]{2}[:\-]){5}[0-9a-f]{2}\b",
    re.IGNORECASE,
)
IPV4_SEARCH_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_mac(value: str | None) -> str | None:
    """Return a MAC as aa:bb:cc:dd:ee:ff, or None if not a MAC."""
    if not value:
        return None
    value = value.strip()
    if not MAC_HEX_RE.match(value):
        return None
    hexchars = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    return ":".join(hexchars[i : i + 2] for i in range(0, 12, 2))


def to_aerohive_mac(mac: str) -> str:
    """Return a MAC in Aerohive's aabb:ccdd:eeff notation."""
    norm = normalize_mac(mac)
    if norm is None:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    hexchars = norm.replace(":", "")
    return f"{hexchars[0:4]}:{hexchars[4:8]}:{hexchars[8:12]}"


def _none_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in NONE_VALUES else value


def parse_number(value: str | None) -> float | None:
    """Extract the first number from a string."""
    if value is None:
        return None
    match = NUMBER_RE.search(value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    num = parse_number(value)
    return int(num) if num is not None else None


def normalize_key(key: str) -> str:
    """'Max client' -> 'max_client', 'Chan(Width)' -> 'chan_width'."""
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if ip.is_unspecified:
        return None
    return value


# --------------------------------------------------------------------------
# Table parsing
# --------------------------------------------------------------------------

SEPARATOR_RE = re.compile(r"^\s*(?:-+(?:\s+-+)*|=+(?:\s+=+)*)\s*$")
TOKEN_RE = re.compile(r"\S+")


@dataclass
class _Span:
    start: int
    end: int  # exclusive


def _assign(tok_start: int, tok_end: int, spans: list[_Span]) -> int:
    """Return index of the column the token belongs to (max overlap)."""
    best_idx = 0
    best_overlap = -1
    best_dist = None
    for idx, span in enumerate(spans):
        overlap = min(tok_end, span.end) - max(tok_start, span.start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = idx
        if overlap <= 0:
            dist = span.start - tok_end if tok_end <= span.start else tok_start - span.end
            if best_overlap <= 0 and (best_dist is None or dist < best_dist):
                best_dist = dist
                best_idx = idx
    return best_idx


def _split_by_spans(line: str, spans: list[_Span]) -> list[str]:
    cells: list[list[str]] = [[] for _ in spans]
    for match in TOKEN_RE.finditer(line):
        cells[_assign(match.start(), match.end(), spans)].append(match.group(0))
    return [" ".join(c) for c in cells]


def _header_spans(header: str) -> list[_Span]:
    """Column spans from a header line (words separated by one space merge)."""
    return [_Span(m.start(), m.end()) for m in re.finditer(r"\S+(?: \S+)*", header)]


@dataclass
class Table:
    """A parsed table with its header and the line index it started on."""

    columns: list[str]
    rows: list[dict[str, str]]
    raw_rows: list[str]
    start_line: int


def parse_tables(text: str, header_re: re.Pattern[str] | None = None) -> list[Table]:
    """Parse every 'header / ----- / rows' table in the text.

    Column boundaries come from the dash separator line (most reliable). If
    the output has no separator, ``header_re`` is used to locate a header
    line and its word positions define the columns.
    """
    lines = clean_output(text).split("\n")
    tables: list[Table] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        spans: list[_Span] | None = None
        header: str | None = None
        first_row = idx + 1
        if (
            SEPARATOR_RE.match(line)
            and idx > 0
            and lines[idx - 1].strip()
            and not SEPARATOR_RE.match(lines[idx - 1])
        ):
            spans = [_Span(m.start(), m.end()) for m in re.finditer(r"-+|=+", line)]
            # Multi-line headers ("Admin" above "Mode", units row below): merge per column.
            header_lines = [lines[idx - 1]]
            back = idx - 2
            while back >= 0 and len(header_lines) < 3:
                above = lines[back]
                if (
                    not above.strip()
                    or SEPARATOR_RE.match(above)
                    or re.search(r"[=:;]", above)
                    or len(above.split()) > len(spans)
                ):
                    break
                header_lines.insert(0, above)
                back -= 1
            if len(header_lines) > 1:
                parts = [_split_by_spans(h, spans) for h in header_lines]
                header = None
                merged = [" ".join(p[i] for p in parts if p[i]).strip() for i in range(len(spans))]
            else:
                header = lines[idx - 1]
                merged = None
        if spans is None:
            idx += 1
            continue

        cells_hdr = merged if merged is not None else _split_by_spans(header or "", spans)
        columns = [normalize_key(c) or f"col{i}" for i, c in enumerate(cells_hdr)]
        # De-duplicate column names.
        seen: dict[str, int] = {}
        for i, col in enumerate(columns):
            if col in seen:
                seen[col] += 1
                columns[i] = f"{col}_{seen[col]}"
            else:
                seen[col] = 0

        rows: list[dict[str, str]] = []
        raw_rows: list[str] = []
        row_idx = first_row
        while row_idx < len(lines):
            row = lines[row_idx]
            if not row.strip():
                break
            # Next table starts: this row is a header followed by a separator.
            if row_idx + 1 < len(lines) and SEPARATOR_RE.match(lines[row_idx + 1]) and not SEPARATOR_RE.match(row):
                break
            if SEPARATOR_RE.match(row) or PROMPT_RE.search("\n" + row.strip()):
                break
            cells = _split_by_spans(row, spans)
            rows.append(dict(zip(columns, cells, strict=True)))
            raw_rows.append(row)
            row_idx += 1
        tables.append(Table(columns=columns, rows=rows, raw_rows=raw_rows, start_line=idx))
        idx = row_idx
    return tables


def parse_table(text: str, header_re: re.Pattern[str] | None = None) -> list[dict[str, str]]:
    """Return the rows of the first table found (empty list if none)."""
    tables = parse_tables(text, header_re)
    return tables[0].rows if tables else []


# --------------------------------------------------------------------------
# key=value / key: value parsing
# --------------------------------------------------------------------------

_KV_RE = re.compile(r"^\s*(?P<key>[^:=;]*[A-Za-z][^:=;]*?)\s*(?P<sep>[:=])\s*(?P<value>.*?)\s*;?\s*$")


# FASTPATH-style "Key.............. value" (switch CLI).
_DOTTED_RE = re.compile(r"^\s*(?P<key>[A-Za-z][^.:=]*?)\s*\.{3,}(?:\s+(?P<value>.*?))?\s*$")


def parse_key_values(text: str) -> dict[str, str]:
    """Parse 'Key: value', 'Key=value; Key2=value2;' and 'Key...... value' output."""
    result: dict[str, str] = {}
    for line in clean_output(text).split("\n"):
        if not line.strip() or SEPARATOR_RE.match(line):
            continue
        dotted = _DOTTED_RE.match(line)
        if dotted:
            key = normalize_key(dotted.group("key"))
            if key and key not in result:
                result[key] = dotted.group("value") or ""
            continue
        # Split ';'-separated chunks only when the line looks like k=v pairs.
        chunks = [c for c in line.split(";")] if "=" in line else [line]
        for chunk in chunks:
            match = _KV_RE.match(chunk)
            if not match:
                continue
            key = normalize_key(match.group("key"))
            if not key or key in result:
                continue
            result[key] = match.group("value").strip()
    return result


# --------------------------------------------------------------------------
# show version
# --------------------------------------------------------------------------

_UPTIME_UNITS = {
    "w": 7 * 86400,
    "d": 86400,
    "h": 3600,
    "m": 60,
    "s": 1,
}
_UPTIME_RE = re.compile(
    r"(\d+)\s*(weeks?|wks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)


def parse_uptime(value: str | None) -> int | None:
    """'0 weeks, 1 days, 2 hours, 3 minutes, 4 seconds' -> seconds."""
    if not value:
        return None
    total = 0
    found = False
    # "hh:mm:ss" component (e.g. "3 days, 01:02:03")
    hms = re.search(r"\b(\d+):(\d{2}):(\d{2})\b", value)
    if hms:
        hours, mins, secs = (int(x) for x in hms.groups())
        total += hours * 3600 + mins * 60 + secs
        found = True
        value = value[: hms.start()] + value[hms.end() :]
    for num, unit in _UPTIME_RE.findall(value):
        unit = unit.lower()
        key = "m" if unit.startswith("mi") or unit == "m" else unit[0]
        total += int(num) * _UPTIME_UNITS[key]
        found = True
    return total if found else None


@dataclass
class VersionInfo:
    """Parsed 'show version' (HiveOS / IQ Engine, or SR-series switch CLI)."""

    version: str | None = None  # e.g. "IQ Engine 10.5r3 build-278650"
    firmware: str | None = None  # e.g. "10.5r3"
    build: str | None = None  # e.g. "278650"
    os_name: str | None = None  # e.g. "IQ Engine" / "HiveOS"
    platform: str | None = None  # e.g. "AP460C"
    bootloader: str | None = None
    uptime_seconds: int | None = None
    serial: str | None = None
    mac: str | None = None  # "Burned In MAC Address" on switches
    raw: dict[str, str] = field(default_factory=dict)


_EXOS_VERSION_RE = re.compile(r"^\s*(?:Switch|Chassis|Slot-\d+)\s*:\s*\S+\s+\S+\s+Rev\b", re.MULTILINE | re.IGNORECASE)


def dialect_from_version(text: str) -> str | None:
    """Which CLI produced this 'show version' output ("hiveos", "switch", "exos"), if it's clear.

    Used when the login prompt didn't identify the device (an unusual prompt
    would otherwise leave a switch being polled with access point commands).
    """
    text = clean_output(text or "")
    if "ExtremeXOS" in text or "IMG:" in text or _EXOS_VERSION_RE.search(text):
        return "exos"
    if "Burned In MAC Address" in text or re.search(r"^\s*Machine (?:Model|Type)\.{3,}", text, re.MULTILINE):
        return "switch"
    if re.search(r"\b(?:HiveOS|IQ Engine)\b", text):
        return "hiveos"
    return None


def parse_show_version(text: str) -> VersionInfo:
    kv = parse_key_values(text)
    info = VersionInfo(raw=kv)
    info.version = kv.get("version") or kv.get("software_version") or kv.get("sw_version")
    if info.version and kv.get("operating_system") and info.version == kv.get("software_version"):
        info.os_name = None
    if info.version:
        fw = re.search(r"(\d+(?:\.\d+)+r\d+[a-z0-9]*)", info.version, re.IGNORECASE)
        if fw:
            info.firmware = fw.group(1)
            name = info.version[: fw.start()].strip()
            info.os_name = name or None
        else:
            num = re.search(r"\d+(?:\.\d+)+\S*", info.version)
            info.firmware = num.group(0) if num else info.version
        build = re.search(r"build[\s\-]*(\d+)", info.version, re.IGNORECASE)
        if build:
            info.build = build.group(1)
    if info.version is None and kv.get("software_version"):
        info.version = kv["software_version"]
    for key in ("platform", "hardware_platform", "machine_model", "model", "product_name"):
        if kv.get(key):
            value = kv[key].split()[0] if key == "platform" else kv[key]
            # "Aerohive SR2208P" -> "SR2208P"
            info.platform = re.sub(r"^(Aerohive|Extreme(\s+Networks)?)\s+", "", value, flags=re.IGNORECASE)
            break
    for key, value in kv.items():
        if key.startswith("bootloader") and info.bootloader is None:
            info.bootloader = value
        elif "serial" in key and info.serial is None:
            info.serial = value
    info.uptime_seconds = parse_uptime(
        kv.get("uptime") or kv.get("system_uptime") or kv.get("system_up_time") or kv.get("up_time")
    )
    info.mac = normalize_mac(kv.get("burned_in_mac_address") or kv.get("base_mac_address"))
    return info


# --------------------------------------------------------------------------
# show interface
# --------------------------------------------------------------------------

_IFACE_NAME_RE = re.compile(
    r"^((mgt|eth|wifi|red|agg|tunnel|usbnet|vlan|lte|br)\S*|\d+/\d+/\d+|\d+(?::\d+)?)$", re.IGNORECASE
)
_USP_RE = re.compile(r"^(\d+)/(\d+)/(\d+)$")  # unit/slot/port (SR-series switch CLI)
_EXOS_PORT_RE = re.compile(r"^\d+(?::\d+)?$")  # ExtremeXOS port / stack slot:port
_CHAN_RE = re.compile(r"^(?P<chan>[^()\s]+)?\s*(?:\((?P<width>[^)]*)\))?$")
_RADIO_RE = re.compile(r"^wifi\d+$")


@dataclass
class Interface:
    """One row of 'show interface'."""

    name: str  # lower-case, e.g. "wifi0.1"
    mac: str | None = None
    mode: str | None = None
    state: str | None = None  # "U" / "D" as reported
    channel: int | None = None
    channel_raw: str | None = None  # e.g. "6", "Down"
    channel_width: str | None = None  # e.g. "20MHz"
    vlan: str | None = None
    radio_profile: str | None = None
    hive: str | None = None
    ssid: str | None = None
    speed: str | None = None  # switch ports, e.g. "1000M"
    duplex: str | None = None
    admin_state: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_port(self) -> bool:
        """Physical Ethernet port (eth0, eth1/1, or 1/0/1 on SR switches; unit 0 = LAG/CPU)."""
        usp = _USP_RE.match(self.name)
        if usp:
            return usp.group(1) != "0"
        if _EXOS_PORT_RE.match(self.name):  # ExtremeXOS: "1".."52", stacks "1:5"
            return True
        return self.name.startswith("eth") and "." not in self.name

    @property
    def is_up(self) -> bool | None:
        if self.state is None:
            return None
        return self.state.upper().startswith("U")

    @property
    def is_radio(self) -> bool:
        return bool(_RADIO_RE.match(self.name))

    @property
    def parent(self) -> str | None:
        """'wifi0.1' -> 'wifi0'."""
        return self.name.split(".", 1)[0] if "." in self.name else None

    @property
    def channel_width_mhz(self) -> int | None:
        return parse_int(self.channel_width)


def _parse_channel(value: str | None) -> tuple[int | None, str | None, str | None]:
    value = _none_if_empty(value)
    if value is None:
        return None, None, None
    match = _CHAN_RE.match(value.replace(" ", ""))
    if not match:
        return None, value, None
    chan_raw = match.group("chan")
    width = _none_if_empty(match.group("width"))
    chan: int | None = None
    if chan_raw and chan_raw.isdigit():
        chan = int(chan_raw)
    return chan, chan_raw, width


_IFACE_HEADER_RE = re.compile(r"^\s*(Name|Port|Interface)\s+MAC\s+addr", re.IGNORECASE)


def _interface_from_cells(cells: dict[str, str]) -> Interface | None:
    name = (cells.get("name") or cells.get("port") or cells.get("interface") or "").strip()
    if not name or not _IFACE_NAME_RE.match(name):
        return None
    iface = Interface(name=name.lower())
    iface.mac = normalize_mac(cells.get("mac_addr") or cells.get("mac"))
    iface.mode = _none_if_empty(cells.get("mode"))
    iface.state = _none_if_empty(cells.get("state"))
    iface.channel, iface.channel_raw, iface.channel_width = _parse_channel(
        cells.get("chan_width") or cells.get("chan") or cells.get("channel")
    )
    iface.vlan = _none_if_empty(cells.get("vlan"))
    iface.radio_profile = _none_if_empty(cells.get("radio"))
    iface.hive = _none_if_empty(cells.get("hive"))
    iface.ssid = _none_if_empty(cells.get("ssid"))
    iface.speed = _none_if_empty(cells.get("speed"))
    iface.duplex = _none_if_empty(cells.get("duplex"))
    iface.admin_state = _none_if_empty(cells.get("admin") or cells.get("admin_state") or cells.get("admin_status"))
    known = {
        "name", "port", "interface", "mac_addr", "mac", "mode", "state", "chan_width", "chan", "channel", "vlan", "radio",
        "hive", "ssid", "speed", "duplex", "admin", "admin_state", "admin_status",
    }  # fmt: skip
    iface.extra = {k: v for k, v in cells.items() if k not in known and v}
    return iface


def _interface_from_tokens(line: str) -> Interface | None:
    """Fallback when no usable table header is present."""
    tokens = line.split()
    if len(tokens) < 4 or not _IFACE_NAME_RE.match(tokens[0]):
        return None
    iface = Interface(name=tokens[0].lower())
    rest = tokens[1:]
    if rest and normalize_mac(rest[0]):
        iface.mac = normalize_mac(rest.pop(0))
    if rest:
        iface.mode = _none_if_empty(rest.pop(0))
    if rest and rest[0].upper() in {"U", "D", "UP", "DOWN"}:
        iface.state = rest.pop(0)
    if rest:
        iface.channel, iface.channel_raw, iface.channel_width = _parse_channel(rest.pop(0))
    if rest:
        iface.vlan = _none_if_empty(rest.pop(0))
    if rest:
        iface.radio_profile = _none_if_empty(rest.pop(0))
    if rest:
        iface.hive = _none_if_empty(rest.pop(0))
    if rest:
        iface.ssid = _none_if_empty(" ".join(rest))
    return iface


def parse_show_interface(text: str) -> dict[str, Interface]:
    """Parse the 'show interface' summary table, keyed by lower-case name."""
    result: dict[str, Interface] = {}
    tables = [
        t for t in parse_tables(text, _IFACE_HEADER_RE) if {"name", "port", "interface"} & set(t.columns)
    ]
    if tables:
        for table in tables:
            for cells, raw in zip(table.rows, table.raw_rows, strict=True):
                try:
                    iface = _interface_from_cells(cells)
                    # Sanity: if the positional split produced no MAC but the
                    # row clearly has one, fall back to token parsing.
                    if iface is not None and iface.mac is None and MAC_SEARCH_RE.search(raw):
                        iface = _interface_from_tokens(raw) or iface
                except Exception:  # noqa: BLE001 - parsers must never raise
                    iface = None
                if iface is not None:
                    result[iface.name] = iface
        return result
    for line in clean_output(text).split("\n"):
        try:
            iface = _interface_from_tokens(line)
        except Exception:  # noqa: BLE001
            iface = None
        if iface is not None:
            result[iface.name] = iface
    return result


# --------------------------------------------------------------------------
# show ssid
# --------------------------------------------------------------------------

_SSID_HEADER_RE = re.compile(r"^\s*No\.?\s+Name\s+", re.IGNORECASE)


@dataclass
class Ssid:
    """One row of 'show ssid'."""

    name: str
    index: int | None = None
    frag: int | None = None
    rts: int | None = None
    dtim_period: int | None = None
    max_client: int | None = None
    mac_filter: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def parse_show_ssid(text: str) -> dict[str, Ssid]:
    """Parse 'show ssid', keyed by SSID name (case preserved)."""
    result: dict[str, Ssid] = {}
    tables = [t for t in parse_tables(text, _SSID_HEADER_RE) if "name" in t.columns]
    if tables:
        for table in tables:
            for cells in table.rows:
                name = (cells.get("name") or "").strip()
                if not name:
                    continue
                if cells.get("no") and not cells["no"].strip().isdigit():
                    continue
                ssid = Ssid(name=name)
                ssid.index = parse_int(cells.get("no"))
                ssid.frag = parse_int(cells.get("frag"))
                ssid.rts = parse_int(cells.get("rts"))
                ssid.dtim_period = parse_int(cells.get("dtim_period") or cells.get("dtim"))
                ssid.max_client = parse_int(cells.get("max_client") or cells.get("max_clients"))
                ssid.mac_filter = _none_if_empty(cells.get("mac_filter"))
                known = {"no", "name", "frag", "rts", "dtim_period", "dtim", "max_client", "max_clients", "mac_filter"}
                ssid.extra = {k: v for k, v in cells.items() if k not in known and v}
                result[name] = ssid
        return result
    # Fallback: "<n> <name ...> <frag> <rts> <dtim> <max> <filter>"
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        if len(tokens) >= 7 and tokens[0].isdigit() and all(t.isdigit() for t in tokens[-5:-1]):
            name = " ".join(tokens[1:-5])
            result[name] = Ssid(
                name=name,
                index=int(tokens[0]),
                frag=int(tokens[-5]),
                rts=int(tokens[-4]),
                dtim_period=int(tokens[-3]),
                max_client=int(tokens[-2]),
                mac_filter=_none_if_empty(tokens[-1]),
            )
    return result


# --------------------------------------------------------------------------
# show station
# --------------------------------------------------------------------------

_IFNAME_CTX_RE = re.compile(r"\bIfname\s*=\s*([A-Za-z0-9.\-]+)", re.IGNORECASE)
_SSID_CTX_RE = re.compile(r"\bSSID\s*=\s*(.+?)\s*:?\s*$", re.IGNORECASE)
_POW_SNR_RE = re.compile(r"(-\d+)\s*(?:dBm)?\s*\(\s*(-?\d+)\s*\)", re.IGNORECASE)
_STATION_HEADER_RE = re.compile(r"^\s*Mac\s*Addr", re.IGNORECASE)


@dataclass
class Station:
    """A connected wireless client."""

    mac: str
    ip: str | None = None
    interface: str | None = None
    ssid: str | None = None
    channel: int | None = None
    tx_rate: str | None = None
    rx_rate: str | None = None
    signal: int | None = None  # dBm (RSSI)
    snr: int | None = None  # dB
    auth_mode: str | None = None
    cipher: str | None = None
    assoc_time: str | None = None
    vlan: str | None = None
    authenticated: bool | None = None
    user_profile: str | None = None
    phy_mode: str | None = None
    hostname: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def radio(self) -> str | None:
        if not self.interface:
            return None
        return self.interface.split(".", 1)[0]


def _first(cells: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        val = _none_if_empty(cells.get(key))
        if val is not None:
            return val
    return None


def _station_from_cells(cells: dict[str, str], raw: str, ctx_iface: str | None, ctx_ssid: str | None) -> Station | None:
    mac = normalize_mac(_first(cells, "mac_addr", "mac_address", "mac", "client_mac"))
    if mac is None:
        tokens = raw.split()
        mac = normalize_mac(tokens[0]) if tokens else None
    if mac is None:
        return None
    sta = Station(mac=mac)
    sta.ip = _valid_ip(_first(cells, "ip_addr", "ip_address", "ip", "ipv4_addr"))
    if sta.ip is None:
        for candidate in IPV4_SEARCH_RE.findall(raw):
            if _valid_ip(candidate):
                sta.ip = candidate
                break
    iface = _first(cells, "ifname", "interface", "if_name", "iface")
    sta.interface = (iface or ctx_iface or "").lower() or None
    sta.ssid = _first(cells, "ssid") or ctx_ssid
    sta.channel = parse_int(_first(cells, "chan", "channel"))
    sta.tx_rate = _first(cells, "tx_rate", "txrate", "tx")
    sta.rx_rate = _first(cells, "rx_rate", "rxrate", "rx")
    pow_value = _first(cells, "pow_snr", "pow", "power", "rssi", "signal")
    match = _POW_SNR_RE.search(pow_value or "") or _POW_SNR_RE.search(raw)
    if match:
        sta.signal = int(match.group(1))
        sta.snr = int(match.group(2))
    else:
        sta.signal = parse_int(pow_value)
        sta.snr = parse_int(_first(cells, "snr"))
    sta.auth_mode = _first(cells, "a_mode", "auth_mode")
    sta.cipher = _first(cells, "cipher")
    sta.assoc_time = _first(cells, "a_time", "assoc_time")
    sta.vlan = _first(cells, "vlan")
    auth = _first(cells, "auth")
    if auth is not None:
        sta.authenticated = auth.lower() in {"yes", "y", "true", "1"}
    sta.user_profile = _first(cells, "upid", "user_profile")
    sta.phy_mode = _first(cells, "phymode", "phy_mode")
    sta.hostname = _first(cells, "hostname", "host_name", "client_hostname")
    known = {
        "mac_addr", "mac_address", "mac", "client_mac", "ip_addr", "ip_address", "ip", "ipv4_addr",
        "ifname", "interface", "if_name", "iface", "ssid", "chan", "channel", "tx_rate", "txrate",
        "tx", "rx_rate", "rxrate", "rx", "pow_snr", "pow", "power", "rssi", "signal", "snr",
        "a_mode", "auth_mode", "cipher", "a_time", "assoc_time", "vlan", "auth", "upid",
        "user_profile", "phymode", "phy_mode", "hostname", "host_name", "client_hostname",
    }  # fmt: skip
    sta.extra = {k: v for k, v in cells.items() if k not in known and v}
    return sta


def parse_show_station(text: str) -> dict[str, Station]:
    """Parse 'show station', keyed by normalised client MAC.

    Handles the classic HiveOS layout where each sub-interface has a context
    line ("Ifname=wifi0.1, Ifindex=13, SSID=Corp:") followed by a table, as
    well as single-table layouts. Unknown formats fall back to picking any
    line that starts with a MAC address.
    """
    result: dict[str, Station] = {}
    cleaned = clean_output(text)
    lines = cleaned.split("\n")

    # Map line index -> (iface, ssid) context in effect at that line.
    contexts: list[tuple[int, str | None, str | None]] = []
    for idx, line in enumerate(lines):
        if_match = _IFNAME_CTX_RE.search(line)
        if if_match:
            ssid_match = _SSID_CTX_RE.search(line)
            contexts.append((idx, if_match.group(1), ssid_match.group(1) if ssid_match else None))

    def ctx_for(line_idx: int) -> tuple[str | None, str | None]:
        current: tuple[str | None, str | None] = (None, None)
        for ctx_idx, iface, ssid in contexts:
            if ctx_idx <= line_idx:
                current = (iface, ssid)
            else:
                break
        return current

    tables = parse_tables(cleaned, _STATION_HEADER_RE)
    parsed_any_table = False
    for table in tables:
        if not any(c.startswith("mac") for c in table.columns):
            continue
        parsed_any_table = True
        ctx_iface, ctx_ssid = ctx_for(table.start_line)
        for cells, raw in zip(table.rows, table.raw_rows, strict=True):
            try:
                sta = _station_from_cells(cells, raw, ctx_iface, ctx_ssid)
            except Exception:  # noqa: BLE001
                sta = None
            if sta is not None:
                result[sta.mac] = sta

    if parsed_any_table:
        return result

    for idx, line in enumerate(lines):
        tokens = line.split()
        if not tokens or normalize_mac(tokens[0]) is None:
            continue
        ctx_iface, ctx_ssid = ctx_for(idx)
        try:
            sta = _station_from_cells({}, line, ctx_iface, ctx_ssid)
        except Exception:  # noqa: BLE001
            sta = None
        if sta is not None:
            result[sta.mac] = sta
    return result


# --------------------------------------------------------------------------
# show interface <name> (detail: radios, ethernet, sub-interfaces, mgt0)
# --------------------------------------------------------------------------

# Ordered candidate key patterns (regex searched against normalised keys).
_DETAIL_FIELDS: dict[str, list[str]] = {
    # radio
    "channel": [r"^operational_channel$", r"^channel$", r"^chan$", r"^current_channel$"],
    "channel_width": [r"channel_width", r"^width$", r"^bandwidth$"],
    "tx_power": [r"^operational_tx_power$", r"^tx_power$", r"^transmit_power$", r"tx_power(?!.*eirp)"],
    "eirp_power": [r"eirp"],
    "noise_floor": [r"noise_floor", r"^noise$"],
    "tx_utilization": [r"^tx_utilization$", r"^tx_util", r"^tx_airtime"],
    "rx_utilization": [r"^rx_utilization$", r"^rx_util", r"^rx_airtime"],
    "interference_utilization": [r"interference_utilization", r"^interference"],
    "total_utilization": [r"^total_utilization$", r"^total_util", r"^channel_utilization$", r"^utilization$"],
    "max_clients": [r"^max_clients?_number$", r"^max_clients?$", r"max_client"],
    "radio_profile": [r"^radio_profile$"],
    "mode": [r"^mode$"],
    "phy_mode": [r"^phymode$", r"^phy_mode$", r"^radio_mode$"],
    # traffic counters
    "rx_bytes": [
        r"^rx_bytes$", r"^rx_octets$", r"^received_bytes$", r"^rx_.*bytes$",
        r"^total_packets_received_octets$", r"^octets_received$", r"received_octets", r"^in_octets$",
    ],
    "tx_bytes": [
        r"^tx_bytes$", r"^tx_octets$", r"^transmitted_bytes$", r"^tx_.*bytes$",
        r"^total_packets_transmitted_octets$", r"^octets_transmitted$", r"transmitted_octets", r"^out_octets$",
    ],
    "rx_packets": [
        r"^rx_packets$", r"^rx_pkts$", r"^rx_frames$", r"^rx_data_frames$",
        r"^total_packets_received_without_errors$",
    ],
    "tx_packets": [
        r"^tx_packets$", r"^tx_pkts$", r"^tx_frames$", r"^tx_data_frames$",
        r"^total_packets_transmitted_successfully$",
    ],
    "rx_errors": [r"^rx_errors$", r"^rx_errs?$", r"^rx_error", r"^total_packets_received_with_mac_errors$"],
    "tx_errors": [r"^tx_errors$", r"^tx_errs?$", r"^tx_error", r"^total_transmit_errors$"],
    "rx_dropped": [r"^rx_dropped$", r"^rx_drops?$", r"^rx_drop", r"^receive_packets_discarded$"],
    "tx_dropped": [r"^tx_dropped$", r"^tx_drops?$", r"^tx_drop", r"^transmit_packets_discarded$"],
    # ethernet / addressing
    "speed": [r"^speed$", r"^link_speed$", r"^operational_speed$"],
    "duplex": [r"^duplex$", r"^operational_duplex$"],
    "admin_state": [r"^admin_state$", r"^admin_status$"],
    "operational_state": [r"^operational_state$", r"^oper_state$", r"^link_state$", r"^link$"],
    "mtu": [r"^mtu$", r"^maximum_transmit_unit$"],
    "ip_address": [r"^ip_addr$", r"^ip_address$", r"^ipv4_addr(ess)?$", r"^ip$"],
    "netmask": [r"^netmask$", r"^mask$", r"^subnet_mask$"],
    "gateway": [r"^default_gateway$", r"^gateway$", r"^default_gw$"],
}
_DETAIL_FLOATS = {
    "tx_power", "eirp_power", "noise_floor", "tx_utilization", "rx_utilization",
    "interference_utilization", "total_utilization",
}  # fmt: skip
_DETAIL_INTS = {
    "channel", "max_clients", "rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
    "rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "mtu",
}  # fmt: skip

COUNTER_FIELDS = (
    "rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
    "rx_errors", "tx_errors", "rx_dropped", "tx_dropped",
)  # fmt: skip


@dataclass
class InterfaceDetail:
    """Parsed 'show interface <name>' (any interface type)."""

    name: str
    # radio
    channel: int | None = None
    channel_width: str | None = None
    tx_power: float | None = None  # dBm
    eirp_power: float | None = None  # dBm
    noise_floor: float | None = None  # dBm
    tx_utilization: float | None = None  # %
    rx_utilization: float | None = None  # %
    interference_utilization: float | None = None  # %
    total_utilization: float | None = None  # %
    max_clients: int | None = None
    radio_profile: str | None = None
    mode: str | None = None
    phy_mode: str | None = None
    # counters
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    rx_dropped: int | None = None
    tx_dropped: int | None = None
    # ethernet / addressing
    speed: str | None = None
    duplex: str | None = None
    admin_state: str | None = None
    operational_state: str | None = None
    mtu: int | None = None
    ip_address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    mapped_keys: list[str] = field(default_factory=list)
    mapped_fields: dict[str, str] = field(default_factory=dict)  # attr -> raw key

    @property
    def speed_mbps(self) -> int | None:
        """'1000Mbps' / '1Gbps' / '100' -> Mbit/s."""
        if not self.speed:
            return None
        num = parse_number(self.speed)
        if num is None:
            return None
        return int(num * 1000) if re.search(r"g", self.speed, re.IGNORECASE) else int(num)


RadioDetail = InterfaceDetail  # backwards compatible name


def match_fields(kv: dict[str, str], fields: dict[str, list[str]]) -> dict[str, tuple[str, str]]:
    """Return {attr: (raw_key, value)} using ordered regex candidates."""
    result: dict[str, tuple[str, str]] = {}
    used: set[str] = set()
    for attr, patterns in fields.items():
        for pattern in patterns:
            regex = re.compile(pattern)
            hit = next(
                (
                    (key, val)
                    for key, val in kv.items()
                    if key not in used and regex.search(key) and _none_if_empty(val) is not None
                ),
                None,
            )
            if hit is not None:
                result[attr] = hit
                used.add(hit[0])
                break
    return result


def parse_show_interface_detail(text: str, name: str) -> InterfaceDetail:
    kv = parse_key_values(text)
    detail = InterfaceDetail(name=name.lower(), raw=kv)
    for attr, (key, value) in match_fields(kv, _DETAIL_FIELDS).items():
        if attr in _DETAIL_FLOATS or attr in _DETAIL_INTS:
            num = parse_number(value.replace(",", ""))
            if num is None:
                continue
            setattr(detail, attr, int(num) if attr in _DETAIL_INTS else num)
        elif attr == "ip_address":
            ip, _, prefix = value.partition("/")
            if _valid_ip(ip) is None:
                continue
            detail.ip_address = ip
            if prefix.isdigit() and detail.netmask is None:
                detail.netmask = str(ipaddress.ip_network(f"0.0.0.0/{prefix}").netmask)
        else:
            setattr(detail, attr, value)
        detail.mapped_keys.append(key)
        detail.mapped_fields[attr] = key
    # Some firmwares report "Channel=6(20MHz)".
    if detail.channel_width is None:
        for key in ("operational_channel", "channel"):
            if key in kv:
                _, _, width = _parse_channel(kv[key])
                if width:
                    detail.channel_width = width
                    break
    return detail


def parse_show_interface_radio(text: str, name: str) -> InterfaceDetail:
    """Backwards compatible alias of parse_show_interface_detail."""
    return parse_show_interface_detail(text, name)


# --------------------------------------------------------------------------
# show cpu / show memory / show hw-info
# --------------------------------------------------------------------------


@dataclass
class SystemStats:
    cpu_total: float | None = None  # %
    cpu_user: float | None = None  # %
    cpu_system: float | None = None  # %
    memory_total_kb: int | None = None
    memory_free_kb: int | None = None
    memory_used_kb: int | None = None
    raw: dict[str, str] = field(default_factory=dict)
    mapped_keys: list[str] = field(default_factory=list)

    @property
    def memory_used_percent(self) -> float | None:
        if not self.memory_total_kb:
            return None
        used = self.memory_used_kb
        if used is None and self.memory_free_kb is not None:
            used = self.memory_total_kb - self.memory_free_kb
        if used is None:
            return None
        return round(used * 100 / self.memory_total_kb, 1)


_SIZE_UNITS_KB = {"b": 1 / 1024, "byte": 1 / 1024, "bytes": 1 / 1024, "k": 1, "kb": 1, "kib": 1,
                  "m": 1024, "mb": 1024, "mib": 1024, "g": 1048576, "gb": 1048576, "gib": 1048576}  # fmt: skip


def _to_kb(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?", value.replace(",", ""))
    if not match:
        return None
    unit = (match.group(2) or "kb").lower()
    factor = _SIZE_UNITS_KB.get(unit, 1)
    return int(float(match.group(1)) * factor)


def parse_show_cpu(text: str) -> SystemStats:
    kv = parse_key_values(text)
    stats = SystemStats(raw=kv)
    for key, value in kv.items():
        if "cpu" not in key and "utilization" not in key and "usage" not in key:
            continue
        num = parse_number(value)
        if num is None:
            continue
        if "total" in key and stats.cpu_total is None:
            stats.cpu_total = num
        elif "user" in key and stats.cpu_user is None:
            stats.cpu_user = num
        elif ("system" in key or "kernel" in key) and stats.cpu_system is None:
            stats.cpu_system = num
        else:
            continue
        stats.mapped_keys.append(key)
    if stats.cpu_total is None:
        if stats.cpu_user is not None and stats.cpu_system is not None:
            stats.cpu_total = round(stats.cpu_user + stats.cpu_system, 2)
        else:
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", clean_output(text))
            if match:
                stats.cpu_total = float(match.group(1))
    return stats


def parse_show_memory(text: str) -> SystemStats:
    kv = parse_key_values(text)
    stats = SystemStats(raw=kv)
    for key, value in kv.items():
        if "total" in key and stats.memory_total_kb is None:
            stats.memory_total_kb = _to_kb(value)
        elif "free" in key and stats.memory_free_kb is None:
            stats.memory_free_kb = _to_kb(value)
        elif "used" in key and stats.memory_used_kb is None:
            stats.memory_used_kb = _to_kb(value)
        else:
            continue
        stats.mapped_keys.append(key)
    if stats.memory_total_kb is None:
        # Linux 'free' style table: header "total used free ..." + "Mem: n n n"
        lines = clean_output(text).split("\n")
        for idx, line in enumerate(lines):
            if line.strip().lower().startswith("mem:"):
                header = lines[idx - 1].lower().split() if idx > 0 else ["total", "used", "free"]
                values = line.split()[1:]
                cols = dict(zip(header, values, strict=False))
                stats.memory_total_kb = _to_kb(cols.get("total"))
                stats.memory_used_kb = _to_kb(cols.get("used"))
                stats.memory_free_kb = _to_kb(cols.get("free"))
                break
    return stats


@dataclass
class HardwareInfo:
    serial: str | None = None
    hw_revision: str | None = None
    product: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    mapped_keys: list[str] = field(default_factory=list)


def parse_show_hw_info(text: str) -> HardwareInfo:
    kv = parse_key_values(text)
    info = HardwareInfo(raw=kv)
    for key, value in kv.items():
        value = _none_if_empty(value)
        if value is None:
            continue
        if info.serial is None and "serial" in key:
            info.serial = value
        elif info.hw_revision is None and ("revision" in key or key in {"hw_rev", "hw_version", "hardware_version"}):
            info.hw_revision = value
        elif info.product is None and key in {"product_name", "product", "model", "model_name"}:
            info.product = value
        else:
            continue
        info.mapped_keys.append(key)
    return info


# --------------------------------------------------------------------------
# Raw values (for the generic "every field" sensors)
# --------------------------------------------------------------------------

_RAW_NUM_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*(%|dBm|dB|Mbps|Kbps|Gbps|bps|MHz|GHz|KB|MB|GB|bytes|ms|seconds|sec|s)?\s*;?$",
    re.IGNORECASE,
)
_RAW_UNIT_CANON = {
    "%": "%", "dbm": "dBm", "db": "dB", "mbps": "Mbit/s", "kbps": "kbit/s", "gbps": "Gbit/s",
    "bps": "bit/s", "mhz": "MHz", "ghz": "GHz", "kb": "kB", "mb": "MB", "gb": "GB",
    "bytes": "B", "ms": "ms", "seconds": "s", "sec": "s", "s": "s",
}  # fmt: skip


def parse_raw_value(value: str | None) -> tuple[float | int | str | None, str | None]:
    """'17 dBm' -> (17, 'dBm'); '1500' -> (1500, None); 'access' -> ('access', None)."""
    value = _none_if_empty(value)
    if value is None:
        return None, None
    match = _RAW_NUM_RE.match(value.replace(",", "") if re.match(r"^-?[\d,]+(\.\d+)?\s*\S*$", value) else value)
    if match:
        num = float(match.group(1))
        unit = _RAW_UNIT_CANON.get((match.group(2) or "").lower())
        return (int(num) if num.is_integer() and "." not in match.group(1) else num), unit
    return value[:255], None


def parse_rate_mbps(value: str | None) -> float | None:
    """Station PHY rates: '72M' -> 72.0, '1.2G' -> 1200.0, '54' -> 54.0."""
    if not value:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG])?", value)
    if not match:
        return None
    num = float(match.group(1))
    unit = (match.group(2) or "m").lower()
    return num / 1000 if unit == "k" else num * 1000 if unit == "g" else num


# --------------------------------------------------------------------------
# show boot-param
# --------------------------------------------------------------------------


@dataclass
class BootParams:
    region_code: str | None = None
    country_code: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    mapped_keys: list[str] = field(default_factory=list)


def parse_show_boot_param(text: str) -> BootParams:
    kv = parse_key_values(text)
    params = BootParams(raw=kv)
    for key, value in kv.items():
        if params.region_code is None and "region" in key:
            params.region_code = _none_if_empty(value)
            params.mapped_keys.append(key)
        elif params.country_code is None and "country" in key:
            params.country_code = _none_if_empty(value)
            params.mapped_keys.append(key)
    return params


# --------------------------------------------------------------------------
# running-config helpers
# --------------------------------------------------------------------------

_SSID_SECOBJ_RE = re.compile(
    r'^\s*ssid\s+(?:"(?P<q>[^"]+)"|(?P<u>\S+))\s+security-object\s+(?:"(?P<oq>[^"]+)"|(?P<ou>\S+))\s*$',
    re.IGNORECASE,
)


def parse_ssid_security_objects(config: str) -> dict[str, str]:
    """Map SSID name -> security-object name from 'show running-config'."""
    result: dict[str, str] = {}
    for line in clean_output(config).split("\n"):
        match = _SSID_SECOBJ_RE.match(line)
        if match:
            ssid = match.group("q") or match.group("u")
            obj = match.group("oq") or match.group("ou")
            result[ssid] = obj
    return result


# --------------------------------------------------------------------------
# SR-series switch CLI: show port all
# --------------------------------------------------------------------------

_PORT_ALL_HEADER_RE = re.compile(r"^\s*Intf\s+", re.IGNORECASE)


def parse_show_port_all(text: str) -> dict[str, Interface]:
    """'show port all' -> Interface per port (name '1/0/1').

    Columns: Intf, Type, Admin Mode, Physical Mode, Physical Status
    ("1000 Full"), Link Status (Up/Down), Link Trap, LACP Mode, Actor Timeout.
    """
    result: dict[str, Interface] = {}
    for table in parse_tables(text, _PORT_ALL_HEADER_RE):
        if "intf" not in table.columns:
            continue
        for cells in table.rows:
            name = (cells.get("intf") or "").strip().lower()
            usp = _USP_RE.match(name)
            if not usp or usp.group(1) == "0":
                continue  # unit 0 = port-channels (LAG) / CPU, not physical ports
            iface = Interface(name=name)
            iface.state = _none_if_empty(cells.get("link_status"))
            iface.admin_state = _none_if_empty(cells.get("admin_mode"))
            iface.mode = _none_if_empty(cells.get("physical_mode"))
            status = _none_if_empty(cells.get("physical_status"))
            if status:
                parts = status.split()
                iface.speed = parts[0] if parts and parts[0][0].isdigit() else None
                iface.duplex = parts[1] if len(parts) > 1 else None
            known = {"intf", "link_status", "admin_mode", "physical_mode", "physical_status"}
            iface.extra = {k: v for k, v in cells.items() if k not in known and v}
            result[name] = iface
    return result


def parse_show_network(text: str) -> InterfaceDetail:
    """'show network' (management interface) -> InterfaceDetail named 'mgt0'."""
    detail = parse_show_interface_detail(text, "mgt0")
    kv = detail.raw
    if detail.operational_state is None and kv.get("interface_status"):
        detail.operational_state = kv["interface_status"]
    return detail


# --------------------------------------------------------------------------
# Switch: PoE (show pse / show poe)
# --------------------------------------------------------------------------

PORT_RE = re.compile(r"^(?:eth|ge|gi|port|xe|te)?\s*\d+(?:/\d+){0,2}$", re.IGNORECASE)
_PORT_TOKEN_RE = re.compile(
    r"(?<![\w/:])(eth\d+(?:/\d+){0,2}|agg\d+|wifi\d+(?:\.\d+)?|mgt\d+|red\d+|\d+/\d+/\d+)(?![\w/:])",
    re.IGNORECASE,
)


def normalize_port(value: str | None) -> str | None:
    """'Eth1/1' -> 'eth1/1', '1/1' -> 'eth1/1', '3' -> 'eth3'."""
    value = _none_if_empty(value)
    if value is None:
        return None
    value = value.strip().lower().replace(" ", "")
    if re.match(r"^(eth|agg|wifi|mgt|red)\d", value) or _USP_RE.match(value):
        return value
    match = re.match(r"^(?:ge|gi|port|xe|te)?(\d+(?:/\d+){0,2})$", value)
    return f"eth{match.group(1)}" if match else None


@dataclass
class PoePort:
    port: str
    power_w: float | None = None
    max_power_w: float | None = None
    status: str | None = None
    admin: str | None = None
    poe_class: str | None = None
    priority: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool | None:
        """Administrative PoE state."""
        value = (self.admin or self.status or "").lower()
        if not value:
            return None
        if any(w in value for w in ("disable", "shutdown", "admin-off", "admin off", "forced-off")):
            return False
        if self.admin and value in {"off", "no", "down"}:
            return False
        return True

    @property
    def delivering(self) -> bool:
        status = (self.status or "").lower()
        if any(w in status for w in ("deliver", "power on", "powered", "on")) and "off" not in status:
            return True
        return (self.power_w or 0) > 0


@dataclass
class PoeSummary:
    budget_w: float | None = None
    used_w: float | None = None
    remaining_w: float | None = None
    raw: dict[str, str] = field(default_factory=dict)


def _watts(value: str | None, header: str = "") -> float | None:
    num = parse_number(value)
    if num is None:
        return None
    unit_src = f"{header} {value}".lower()
    if "mw" in unit_src:
        return round(num / 1000, 3)
    return num


def parse_show_poe(text: str) -> tuple[PoeSummary, dict[str, PoePort]]:
    summary = PoeSummary()
    ports: dict[str, PoePort] = {}
    cleaned = clean_output(text)
    # Summary lines are the key/value lines outside the table.
    kv = {}
    for line in cleaned.split("\n"):
        stripped = line.strip()
        if _PORT_TOKEN_RE.match(stripped):
            continue
        if re.match(r"^[A-Za-z][^:=]*[:=]", stripped) or _DOTTED_RE.match(line):
            for key, value in parse_key_values(line).items():
                kv.setdefault(key, value)
    summary.raw = kv
    for key, value in kv.items():
        if summary.used_w is None and any(w in key for w in ("consum", "used", "usage")):
            summary.used_w = _watts(value, key)
        elif summary.remaining_w is None and any(w in key for w in ("remain", "free", "available_power", "left")):
            summary.remaining_w = _watts(value, key)
        elif summary.budget_w is None and ("budget" in key or ("total" in key and "power" in key) or "max" in key):
            summary.budget_w = _watts(value, key)
    if summary.remaining_w is None and summary.budget_w is not None and summary.used_w is not None:
        summary.remaining_w = round(summary.budget_w - summary.used_w, 2)

    for table in parse_tables(cleaned, re.compile(r"^\s*(Port|Interface)\b", re.IGNORECASE)):
        cols = table.columns
        if not table.rows:
            continue
        port_col = next((c for c in cols if c in {"port", "interface", "ifname", "name", "intf"}), None)
        if port_col is None:
            port_col = next(
                (c for c in cols if all(normalize_port(r.get(c)) for r in table.rows if r.get(c))), None
            )
        if port_col is None:
            continue

        def col(*words: str, exclude: tuple[str, ...] = (), cols: list[str] = cols) -> str | None:
            return next((c for c in cols if any(w in c for w in words) and not any(x in c for x in exclude)), None)

        power_col = (
            next((c for c in cols if c in {"power", "power_mw", "power_w", "output_power", "output_power_mw",
                                           "output_power_w", "consumed_power", "power_consumed"}), None)
            or col("consum", "actual", "used", exclude=("max", "limit", "budget", "alloc"))
            or col("power", exclude=("max", "limit", "budget", "alloc", "class", "prior", "high", "current", "voltage"))
        )  # fmt: skip
        max_col = col("max", "limit", "alloc", "budget")
        admin_col = col("admin", "enable", exclude=("status",)) or (cols and next((c for c in cols if c == "mode"), None))
        status_col = next((c for c in cols if c in {"status", "port_status", "state", "detection_status"}), None) or col(
            "status", "state", "oper", "detect", exclude=("admin", "fault")
        )
        class_col = col("class")
        prio_col = col("prior")
        used = {port_col, power_col, max_col, admin_col, status_col, class_col, prio_col}
        for row in table.rows:
            port = normalize_port(row.get(port_col))
            if port is None:
                continue
            entry = PoePort(port=port)
            entry.power_w = _watts(row.get(power_col), power_col or "") if power_col else None
            entry.max_power_w = _watts(row.get(max_col), max_col or "") if max_col else None
            entry.admin = _none_if_empty(row.get(admin_col)) if admin_col else None
            entry.status = _none_if_empty(row.get(status_col)) if status_col else None
            entry.poe_class = _none_if_empty(row.get(class_col)) if class_col else None
            entry.priority = _none_if_empty(row.get(prio_col)) if prio_col else None
            entry.extra = {k: v for k, v in row.items() if k not in used and v}
            ports[port] = entry
    return summary, ports


def poe_port_count(version: VersionInfo) -> int | None:
    """'Aerohive SR2208P: 8 GE POE+ ports, ...' -> 8."""
    for key in ("machine_type", "system_description"):
        match = re.search(r"(\d+)\s*(?:GE|FE|x)?\s*PoE\+?\s*ports", version.raw.get(key, ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_poe_port_detail(text: str, port: str) -> PoePort | None:
    """'show poe port <u/s/p>'-style key/value output for one port (best effort)."""
    kv = parse_key_values(text)
    if not kv:
        return None
    entry = PoePort(port=port)
    used: set[str] = set()
    for key, value in kv.items():
        if entry.power_w is None and "power" in key and any(w in key for w in ("output", "consum", "used", "actual", "draw")):
            entry.power_w = _watts(value, key)
        elif entry.max_power_w is None and "power" in key and any(w in key for w in ("max", "limit", "alloc")):
            entry.max_power_w = _watts(value, key)
        elif entry.admin is None and ("admin" in key or key in {"mode", "poe_mode", "port_mode"}):
            entry.admin = _none_if_empty(value)
        elif entry.status is None and any(w in key for w in ("status", "state", "detection")):
            entry.status = _none_if_empty(value)
        elif entry.poe_class is None and "class" in key:
            entry.poe_class = _none_if_empty(value)
        elif entry.priority is None and "prior" in key:
            entry.priority = _none_if_empty(value)
        else:
            continue
        used.add(key)
    entry.extra = {k: v for k, v in kv.items() if k not in used and v}
    if entry.power_w is None and entry.status is None and entry.admin is None:
        return None
    return entry


# --------------------------------------------------------------------------
# Switch / router: MAC address table
# --------------------------------------------------------------------------


@dataclass
class FdbEntry:
    mac: str
    port: str | None = None
    vlan: int | None = None
    entry_type: str | None = None


_FDB_SELF = {"self", "cpu", "mgmt", "management", "local", "system"}


def parse_mac_address_table(text: str) -> list[FdbEntry]:
    """Tolerant parser: any line with a MAC address and (ideally) a port."""
    entries: list[FdbEntry] = []
    seen: set[tuple[str, str | None]] = set()
    for line in clean_output(text).split("\n"):
        mac_match = MAC_SEARCH_RE.search(line)
        if not mac_match:
            continue
        mac = normalize_mac(mac_match.group(0))
        if mac is None:
            continue
        rest = line[: mac_match.start()] + " " + line[mac_match.end() :]
        port_match = _PORT_TOKEN_RE.search(rest)
        port = port_match.group(1).lower() if port_match else None
        tokens = rest.split()
        entry_type = next((t.lower() for t in tokens if t.lower() in _FDB_SELF | {"dynamic", "static", "learned", "learning"}), None)
        if entry_type in _FDB_SELF or (port and port.startswith("mgt")):
            continue
        vlan = None
        for tok in tokens:
            if tok.isdigit() and 1 <= int(tok) <= 4094:
                vlan = int(tok)
                break
        if (mac, port) in seen:
            continue
        seen.add((mac, port))
        entries.append(FdbEntry(mac=mac, port=port, vlan=vlan, entry_type=entry_type))
    return entries


_AH_HOSTNAME_RE = re.compile(r"^ah-[0-9a-f]{6}", re.IGNORECASE)


def is_aerohive_hostname(hostname: str | None) -> bool:
    """Default HiveOS hostname 'AH-xxxxxx' (last 6 MAC digits)."""
    return bool(hostname and _AH_HOSTNAME_RE.match(hostname))


# --------------------------------------------------------------------------
# LLDP neighbours
# --------------------------------------------------------------------------


@dataclass
class LldpNeighbor:
    local_port: str
    system_name: str | None = None
    chassis_id: str | None = None
    port_id: str | None = None
    port_description: str | None = None
    system_description: str | None = None
    management_address: str | None = None
    capabilities: str | None = None
    capabilities_enabled: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def is_access_point(self) -> bool:
        caps = f"{self.capabilities or ''} {self.capabilities_enabled or ''}".lower()
        return "wlan" in caps or is_aerohive_hostname(self.system_name)

    @property
    def is_network_device(self) -> bool:
        """A switch/router (uplink), not an end device or AP."""
        caps = f"{self.capabilities or ''}".lower()
        return not self.is_access_point and ("bridge" in caps or "router" in caps)


def _lldp_from_dict(d: dict[str, str]) -> LldpNeighbor | None:
    def pick(*words: str, exclude: tuple[str, ...] = ()) -> str | None:
        exclude = (*exclude, "subtype")
        for key, value in d.items():
            if any(w in key for w in words) and not any(x in key for x in exclude):
                val = _none_if_empty(value)
                if val is not None:
                    return val
        return None

    local = pick("local_port", "local_interface", "local_intf", "interface", "ifname", "local")
    if local is None:
        local = d.get("port") if normalize_port(d.get("port")) else None
    local = normalize_port(local) if local else None
    if local is None:
        return None
    neighbor = LldpNeighbor(
        local_port=local,
        system_name=pick("system_name", "sys_name", "device_id", "neighbor"),
        chassis_id=pick("chassis"),
        port_id=pick("port_id", "remote_port", "neighbor_port", "port_identifier", exclude=("local",)),
        port_description=pick("port_desc", "port_description"),
        system_description=pick("system_description", "system_desc"),
        management_address=next(
            (v for v in (pick("management"), pick("mgmt"), d.get("address"), pick("ip_addr")) if v and _valid_ip(v)),
            None,
        ),
        capabilities=pick("capabilities_supported", "capabilit", exclude=("enabled",)) or pick("capabilit"),
        capabilities_enabled=pick("capabilities_enabled"),
        raw=d,
    )
    if not (neighbor.chassis_id or neighbor.system_name or neighbor.port_id):
        return None  # empty row (port without neighbour)
    return neighbor


def parse_lldp_neighbors(text: str) -> dict[str, LldpNeighbor]:
    result: dict[str, LldpNeighbor] = {}
    cleaned = clean_output(text)
    for table in parse_tables(cleaned, re.compile(r"^\s*Local\s+\S", re.IGNORECASE)):
        for row in table.rows:
            neighbor = _lldp_from_dict(row)
            if neighbor is not None:
                result[neighbor.local_port] = neighbor
    if result:
        return result
    # Block format: "Key: value" lines. Split on "Local Interface:" headers
    # when present (SR switch detail output), otherwise on blank lines.
    if re.search(r"^\s*Local\s+(Interface|Port)\s*:", cleaned, re.IGNORECASE | re.MULTILINE):
        blocks = re.split(r"(?=^\s*Local\s+(?:Interface|Port)\s*:)", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    else:
        blocks = re.split(r"\n\s*\n", cleaned)
    for block in blocks:
        neighbor = _lldp_from_dict(parse_key_values(block))
        if neighbor is not None:
            result.setdefault(neighbor.local_port, neighbor)
    return result


# --------------------------------------------------------------------------
# ExtremeXOS (Summit X450e and other EXOS switches)
# --------------------------------------------------------------------------
# Formats follow the ExtremeXOS 12.x-16.x command reference examples.

_EXOS_PORT_STATES = {"E", "D", "F"}  # Enabled, Disabled, ...
_EXOS_LINK_STATES = {"A", "R", "NP", "L"}  # Active, Ready, Not Present, Loopback
_EXOS_OK_STATES = ("OPERATIONAL", "MASTER", "BACKUP", "STANDBY", "IN SYNC")
_EXOS_BAD_STATES = ("FAIL", "ERROR", "INIT", "BOOTING", "DOWN", "NOT ", "POWER")


def _first_column(value: str | None) -> str | None:
    """'primary.cfg   primary.cfg' (MSM/stack two-column layout) -> 'primary.cfg'.

    'Factory Default' (a two-word value) is kept as-is.
    """
    value = _none_if_empty(value)
    if value is None:
        return None
    parts = value.split()
    if len(parts) >= 2 and (parts[0].endswith(".cfg") or parts[0].lower() in {"primary", "secondary"}):
        return parts[0]
    if len(parts) >= 2 and re.match(r"^\d+(?:\.\d+)+$", parts[0]):
        return parts[0]
    return value


@dataclass
class ExosSwitchInfo:
    """'show switch' (and 'show version') on ExtremeXOS."""

    sysname: str | None = None
    system_type: str | None = None
    mac: str | None = None
    serial: str | None = None
    uptime_seconds: int | None = None
    boot_count: int | None = None
    boot_time: str | None = None
    current_state: str | None = None
    health: str | None = None
    image_selected: str | None = None
    image_booted: str | None = None
    primary_version: str | None = None
    secondary_version: str | None = None
    config_selected: str | None = None
    config_booted: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    mapped_keys: list[str] = field(default_factory=list)

    @property
    def booted_version(self) -> str | None:
        booted = (self.image_booted or "").lower()
        if booted.startswith("secondary"):
            return self.secondary_version
        return self.primary_version

    @property
    def image_pending(self) -> str | None:
        """A different image is selected for the next reboot (e.g. a staged upgrade)."""
        if self.image_booted and self.image_selected and self.image_booted != self.image_selected:
            return self.image_selected
        return None

    @property
    def boot_problems(self) -> list[str]:
        """Signs the switch did not come up the way it was configured to."""
        problems = []
        booted = (self.config_booted or "").lower()
        if booted and any(w in booted for w in ("default", "factory", "none")):
            problems.append(f"booted with {self.config_booted} instead of a saved configuration")
        elif self.config_booted and self.config_selected and self.config_booted != self.config_selected:
            problems.append(f"booted {self.config_booted} but {self.config_selected} is selected")
        state = (self.current_state or "").upper()
        if state and any(w in state for w in _EXOS_BAD_STATES) and not any(w in state for w in _EXOS_OK_STATES):
            problems.append(f"state is {self.current_state}")
        return problems


def parse_exos_show_switch(text: str) -> ExosSwitchInfo:
    kv = parse_key_values(text)
    info = ExosSwitchInfo(raw=kv)
    plain = {"sysname": "sysname", "system_type": "system_type", "boot_time": "boot_time",
             "syshealth_check": "health"}  # fmt: skip
    columns = {"current_state": "current_state", "image_selected": "image_selected",
               "image_booted": "image_booted", "primary_ver": "primary_version",
               "secondary_ver": "secondary_version", "config_selected": "config_selected",
               "config_booted": "config_booted"}  # fmt: skip
    for key, attr in plain.items():
        if _none_if_empty(kv.get(key)) is not None:
            setattr(info, attr, kv[key])
            info.mapped_keys.append(key)
    for key, attr in columns.items():
        value = kv.get(key)
        if _none_if_empty(value) is None:
            continue
        if key == "current_state":
            # "MASTER                       BACKUP (In Sync)" -> "MASTER"
            value = re.split(r"\s{2,}", value.strip())[0]
        else:
            value = _first_column(value)
        setattr(info, attr, value)
        info.mapped_keys.append(key)
    info.mac = normalize_mac(kv.get("system_mac"))
    info.boot_count = parse_int(kv.get("boot_count"))
    info.uptime_seconds = parse_uptime(kv.get("system_uptime"))
    info.mapped_keys += [k for k in ("system_mac", "boot_count", "system_uptime") if k in kv]
    return info


def parse_exos_version(text: str) -> tuple[str | None, str | None]:
    """'show version' -> (serial number, image version).

    'Switch : 800153-00-09 0905G-00001 Rev 9.0 BootROM: 1.0.5.7 IMG: 12.6.3.4'
    (standalone; 'Slot-1' on a stack, 'Chassis' on a modular switch) and
    'Image : ExtremeXOS version 12.6.3.4 v1263b4 by release-manager'.
    """
    serial = image = None
    for line in clean_output(text).split("\n"):
        match = re.match(r"^\s*(Switch|Chassis|Slot-1)\s*:\s*(\S+)\s+(\S+)\s+Rev", line, re.IGNORECASE)
        if match and serial is None:
            serial = match.group(3)
        img = re.match(r"^\s*Image\s*:\s*ExtremeXOS version\s+(\S+)", line, re.IGNORECASE)
        if img and image is None:
            image = img.group(1)
    return serial, image


def _exos_port_token(token: str) -> str | None:
    """'1:2' / '12' / '3(0010)' (port with a tag) -> port name."""
    token = re.sub(r"\(.*\)$", "", token.strip())
    return token if _EXOS_PORT_RE.match(token) else None


def parse_exos_ports(text: str) -> dict[str, Interface]:
    """'show ports no-refresh' (Port Summary), parsed by tokens from the right.

    Row: <port> [display string] <vlan name | (n VLANs)> <port state> <link state> [ELSM D] [speed] [duplex]
    """
    result: dict[str, Interface] = {}
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        if len(tokens) < 3 or not _EXOS_PORT_RE.match(tokens[0]):
            continue
        name, rest = tokens[0], tokens[1:]
        iface = Interface(name=name)
        if rest and rest[-1].upper() in {"FULL", "HALF"}:
            iface.duplex = rest.pop().capitalize()
        if rest and rest[-1].isdigit():
            iface.speed = rest.pop()
        if len(rest) >= 2 and rest[-1] == "D" and rest[-2] in _EXOS_LINK_STATES:
            rest.pop()  # "A D": ELSM enabled but not up
            iface.extra["elsm"] = "down"
        if not rest or rest[-1] not in _EXOS_LINK_STATES:
            continue
        link = rest.pop()
        iface.state = "Up" if link == "A" else "Down"
        iface.extra["link_state"] = link
        if rest and rest[-1] in _EXOS_PORT_STATES:
            iface.admin_state = "Enable" if rest.pop() == "E" else "Disable"
        if rest:
            vlan = rest.pop()
            count = re.match(r"^\((\d+)\)$", vlan)
            iface.vlan = f"{count.group(1)} VLANs" if count else vlan
        if rest:
            iface.extra["display_string"] = " ".join(rest)
        result[name] = iface
    return result


def parse_exos_port_statistics(text: str) -> dict[str, InterfaceDetail]:
    """'show ports statistics no-refresh': Tx Pkt, Tx Byte, Rx Pkt, Rx Byte, Rx Bcast, Rx Mcast."""
    result: dict[str, InterfaceDetail] = {}
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        if len(tokens) < 6 or not _EXOS_PORT_RE.match(tokens[0]):
            continue
        numbers = [t for t in tokens[1:] if t.isdigit()]
        if len(numbers) < 4:
            continue
        detail = InterfaceDetail(name=tokens[0])
        detail.tx_packets, detail.tx_bytes, detail.rx_packets, detail.rx_bytes = (int(n) for n in numbers[:4])
        result[tokens[0]] = detail
    return result


def parse_exos_inline_power(text: str) -> PoeSummary:
    """'show inline-power': budgeted and measured power.

    Standalone: 'Operational   405 W   0 W   Disabled' (budgeted, measured);
    modular/stack: one row per slot ('3  Enabled  Operational  50 W  9 W').
    Budgets in parentheses aren't allocated and are skipped.
    """
    summary = PoeSummary(raw=parse_key_values(text))
    for line in clean_output(text).split("\n"):
        if "(" in line and re.search(r"\(\s*\d+\s*W\s*\)", line):
            continue
        watts = re.findall(r"(\d+(?:\.\d+)?)\s*W\b", line)
        if len(watts) >= 2 and not re.match(r"^\s*(System|Redundant)", line, re.IGNORECASE):
            summary.budget_w = (summary.budget_w or 0.0) + float(watts[0])
            summary.used_w = (summary.used_w or 0.0) + float(watts[1])
    if summary.budget_w is not None and summary.used_w is not None:
        summary.remaining_w = round(summary.budget_w - summary.used_w, 2)
    return summary


def parse_exos_inline_power_info(text: str) -> dict[str, PoePort]:
    """'show inline-power info ports': Port State Class Volts Curr(mA) Power(W) Fault."""
    result: dict[str, PoePort] = {}
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        if len(tokens) < 6 or not _EXOS_PORT_RE.match(tokens[0]):
            continue
        port, state, poe_class = tokens[0], tokens[1], tokens[2]
        numbers = tokens[3:6]
        if not all(parse_number(n) is not None for n in numbers):
            continue
        entry = PoePort(port=port, status=state, poe_class=None if set(poe_class) <= {"-"} else poe_class)
        entry.power_w = parse_number(numbers[2])
        entry.admin = "Disable" if state.lower().startswith("disabled") else "Enable"
        entry.extra = {
            "output_voltage_v": numbers[0],
            "output_current_ma": numbers[1],
            "fault_status": " ".join(tokens[6:]) or "None",
        }
        result[port] = entry
    return result


def parse_exos_fdb(text: str) -> list[FdbEntry]:
    """'show fdb': MAC, VLAN name(id), age, flags, port (last column, e.g. '1:2' or '3(0010)')."""
    entries: list[FdbEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in clean_output(text).split("\n"):
        mac_match = MAC_SEARCH_RE.search(line)
        if not mac_match:
            continue
        tokens = line.split()
        port = _exos_port_token(tokens[-1]) if tokens else None
        mac = normalize_mac(mac_match.group(0))
        if not port or not mac or (mac, port) in seen:
            continue  # CPU / self entries, or the same MAC on another VLAN
        seen.add((mac, port))
        vlan = re.search(r"\((\d{1,4})\)", line[mac_match.end():])
        flags = line[mac_match.end():]
        entry_type = "static" if re.search(r"\bs\w*\b", flags.split(")", 1)[-1]) else "dynamic"
        entries.append(FdbEntry(mac=mac, port=port, vlan=int(vlan.group(1)) if vlan else None, entry_type=entry_type))
    return entries


def parse_exos_lldp_neighbors(text: str) -> dict[str, LldpNeighbor]:
    """'show lldp neighbors': Port, Chassis ID, Port ID, TTL, Age [, System Name (EXOS 22.1+)]."""
    result: dict[str, LldpNeighbor] = {}
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        if len(tokens) < 5 or not _EXOS_PORT_RE.match(tokens[0]):
            continue
        if not (tokens[3].isdigit() and tokens[4].isdigit()):
            continue
        chassis = re.sub(r"^\(\d+\.\d+\)", "", tokens[1])  # "(5.1)10.201.41.146" -> IP
        result[tokens[0]] = LldpNeighbor(
            local_port=tokens[0],
            chassis_id=chassis,
            port_id=tokens[2],
            system_name=" ".join(tokens[5:]) or None,
            raw={"line": line.strip()},
        )
    return result


def _lldp_values(block: str) -> dict[str, str]:
    values: dict[str, str] = {}
    # Long values wrap with a trailing backslash.
    block = re.sub(r"\\\n\s*", "", block)
    for line in block.split("\n"):
        line = line.strip().lstrip("-").strip()
        for part in line.split(";"):
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            key = normalize_key(key)
            if key and key not in values:
                values[key] = value.strip().strip('"')
    return values


def _lldp_from_values(port: str, values: dict[str, str]) -> LldpNeighbor:
    name = values.get("system_name")
    model = " ".join(v for v in (values.get("med_manufacturer_name"), values.get("med_model_name")) if v)
    return LldpNeighbor(
        local_port=port,
        system_name=name,
        chassis_id=values.get("chassis_id"),
        port_id=values.get("port_id"),
        port_description=values.get("port_description"),
        system_description=values.get("system_description") or (model or None),
        management_address=_valid_ip(values.get("management_address")),
        capabilities=values.get("system_capabilities"),
        capabilities_enabled=values.get("enabled_capabilities"),
        raw=values,
    )


def parse_exos_lldp_detail(text: str, port: str) -> LldpNeighbor | None:
    """One port's 'show lldp ports N neighbors detailed'."""
    values = _lldp_values(clean_output(text))
    return _lldp_from_values(port, values) if values else None


def parse_exos_lldp_detail_all(text: str) -> dict[str, LldpNeighbor]:
    """'show lldp neighbors detailed': blocks starting 'LLDP Port 1:2 detected 1 neighbor'."""
    result: dict[str, LldpNeighbor] = {}
    parts = re.split(r"^\s*LLDP Port\s+(\S+)\s+detected\s+\d+\s+neighbors?\s*$", clean_output(text), flags=re.M | re.I)
    for port, block in zip(parts[1::2], parts[2::2], strict=False):
        values = _lldp_values(block)
        if values and _EXOS_PORT_RE.match(port):
            result[port] = _lldp_from_values(port, values)
    return result


@dataclass
class ExosVlan:
    name: str
    vid: int
    ip_address: str | None = None
    prefix: int | None = None
    active_ports: int | None = None
    total_ports: int | None = None
    virtual_router: str | None = None


_VLAN_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<vid>\d{1,4})\s+"
    r"(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s*/\s*(?P<prefix>\d{1,2})\s+)?"
    r"(?P<flags>\S+)\s+(?P<proto>\S+)\s+(?P<active>\d+)\s*/\s*(?P<total>\d+)\s*(?P<vr>\S+)?\s*$"
)


def parse_exos_vlans(text: str) -> dict[str, ExosVlan]:
    """'show vlan' summary: name, VID, IP/prefix, flags, protocol, active/total ports, VR."""
    result: dict[str, ExosVlan] = {}
    for line in clean_output(text).split("\n"):
        match = _VLAN_ROW_RE.match(line.strip())
        if not match:
            continue
        result[match.group("name")] = ExosVlan(
            name=match.group("name"),
            vid=int(match.group("vid")),
            ip_address=match.group("ip"),
            prefix=int(match.group("prefix")) if match.group("prefix") else None,
            active_ports=int(match.group("active")),
            total_ports=int(match.group("total")),
            virtual_router=match.group("vr"),
        )
    return result


@dataclass
class EnvironmentInfo:
    """Temperature, fans and power supplies (ExtremeXOS)."""

    temperature_c: float | None = None
    temperature_status: str | None = None
    fans_state: str | None = None
    fans: dict[str, str] = field(default_factory=dict)
    fan_rpm: dict[str, int] = field(default_factory=dict)
    power_supplies: dict[str, str] = field(default_factory=dict)
    power_usage_w: float | None = None

    @property
    def power_state(self) -> str | None:
        if not self.power_supplies:
            return None
        states = {v for v in self.power_supplies.values() if v.lower() != "empty"} or set(self.power_supplies.values())
        return states.pop() if len(states) == 1 else ", ".join(f"{k}: {v}" for k, v in self.power_supplies.items())

    @property
    def problems(self) -> list[str]:
        out = []
        if self.temperature_status and self.temperature_status.lower() != "normal":
            out.append(f"temperature {self.temperature_status}")
        if self.fans_state and self.fans_state.lower() not in {"operational", "empty"}:
            out.append(f"fans {self.fans_state}")
        out += [f"{name} {state}" for name, state in self.fans.items()
                if not state.lower().startswith(("operational", "empty"))]  # fmt: skip
        out += [f"{name} {state}" for name, state in self.power_supplies.items()
                if re.search(r"fail|fault|unsupported|no power", state, re.IGNORECASE)]  # fmt: skip
        return out


_TEMP_STATUS_ORDER = {"normal": 0, "warning": 1, "alarm": 2, "critical": 3, "error": 3, "fault": 3}


def parse_exos_temperature(text: str) -> tuple[float | None, str | None]:
    """Highest temperature and worst status over all rows (one per slot on a stack)."""
    best: tuple[float, str] | None = None
    for line in clean_output(text).split("\n"):
        match = re.search(r"(-?\d+(?:\.\d+)?)\s+(Normal|Warning|Critical|Alarm|Error|Fault)\b", line, re.IGNORECASE)
        if not match:
            continue
        temp, status = float(match.group(1)), match.group(2).capitalize()
        if best is None or (_TEMP_STATUS_ORDER.get(status.lower(), 0), temp) > (
            _TEMP_STATUS_ORDER.get(best[1].lower(), 0), best[0]
        ):
            best = (temp, status)
    return best if best else (None, None)


def parse_exos_fans(text: str) -> tuple[str | None, dict[str, str]]:
    """'show fans': tray state(s) and 'Fan-N: Operational at 4800 RPM' per fan."""
    states: list[str] = []
    fans: dict[str, str] = {}
    tray = ""
    for line in clean_output(text).split("\n"):
        header = re.match(r"^\s*(FanTray(?:-\d+)?)\s+information", line, re.IGNORECASE)
        if header:
            tray = header.group(1)
            continue
        state = re.match(r"^\s*State\s*:\s*(.+?)\s*$", line)
        if state:
            states.append(state.group(1))
            continue
        fan = re.search(r"\b(Fan-\d+)\s*:\s*(.+?)\s*$", line)
        if fan:
            name = f"{tray} {fan.group(1)}".strip() if tray and tray.lower() != "fantray" else fan.group(1)
            fans[name] = fan.group(2)
    real = [s for s in states if s.lower() != "empty"] or states
    state_out = None
    if real:
        bad = [s for s in real if s.lower() != "operational"]
        state_out = bad[0] if bad else real[0]
    return state_out, fans


def fan_rpms(fans: dict[str, str]) -> dict[str, int]:
    return {name: int(m.group(1)) for name, state in fans.items() if (m := re.search(r"(\d+)\s*RPM", state))}


def parse_exos_power(text: str) -> tuple[dict[str, str], float | None]:
    """'show power': State per power supply, and System Power Usage (W)."""
    supplies: dict[str, str] = {}
    usage: float | None = None
    current = "PSU"
    for line in clean_output(text).split("\n"):
        header = re.match(r"^\s*(?:(Slot-\d+)\s+)?PowerSupply\s+(\d+)", line, re.IGNORECASE)
        if header:
            current = f"{header.group(1) + ' ' if header.group(1) else ''}PSU {header.group(2)}"
            continue
        state = re.match(r"^\s*State\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if state:
            supplies[current] = re.sub(r"\s{2,}", " ", state.group(1))
            continue
        total = re.match(r"^\s*System Power Usage\s*:\s*([\d.]+)\s*W", line, re.IGNORECASE)
        if total:
            usage = float(total.group(1))
    return supplies, usage


def parse_exos_cpu(text: str) -> SystemStats:
    """'show cpu-monitoring': the 'System' row ('Slot-1 System' on a stack); first value that isn't n/a."""
    stats = SystemStats()
    for line in clean_output(text).split("\n"):
        tokens = line.split()
        index = next((i for i, t in enumerate(tokens[:2]) if t.lower() == "system"), None)
        if index is None:
            continue
        numbers = [n for t in tokens[index + 1 :] if (n := parse_number(t)) is not None]
        if numbers:
            stats.cpu_total = numbers[0]
            break
    return stats


# --------------------------------------------------------------------------
# running-config summary (SSIDs, security, hidden)
# --------------------------------------------------------------------------


@dataclass
class RunningConfigSummary:
    ssid_security_objects: dict[str, str] = field(default_factory=dict)
    hidden_ssids: set[str] = field(default_factory=set)
    security_suites: dict[str, str] = field(default_factory=dict)  # security-object -> protocol suite
    pse_shutdown_ports: set[str] = field(default_factory=set)
    shutdown_ports: set[str] = field(default_factory=set)
    uptime_seconds: int | None = None  # SR switch: '!System Up Time "0 days 1 hrs 35 mins"'
    ssid_bindings: dict[str, set[str]] = field(default_factory=dict)  # radio -> SSIDs ("interface wifi0 ssid X")

    def ssid_security(self, ssid: str) -> str | None:
        obj = self.ssid_security_objects.get(ssid, ssid)
        return self.security_suites.get(obj)


_ARG = r'(?:"([^"]+)"|(\S+))'
_BIND_RE = re.compile(rf"^\s*interface\s+(wifi\d+)\s+ssid\s+{_ARG}(\s+\S.*)?$", re.IGNORECASE)
_HIDE_RE = re.compile(rf"^\s*ssid\s+{_ARG}\s+hide-ssid\b", re.IGNORECASE)
_SUITE_RE = re.compile(rf"^\s*security-object\s+{_ARG}\s+security\s+protocol-suite\s+(\S+)", re.IGNORECASE)
_PSE_OFF_RE = re.compile(r"^\s*interface\s+(eth\S+)\s+pse\s+shutdown\b", re.IGNORECASE)
_PORT_OFF_RE = re.compile(r"^\s*interface\s+(eth\S+)\s+shutdown\s*$", re.IGNORECASE)
_UPTIME_COMMENT_RE = re.compile(r'^\s*!\s*System Up Time\s+"?([^"]+)"?', re.IGNORECASE)
_BLOCK_IF_RE = re.compile(r"^\s*interface\s+(\d+/\d+/\d+)\s*$", re.IGNORECASE)


def parse_running_config_summary(config: str) -> RunningConfigSummary:
    """Extract SSID/security/port state. Never returns key material."""
    summary = RunningConfigSummary(ssid_security_objects=parse_ssid_security_objects(config))
    block_port: str | None = None  # SR switch: "interface 1/0/4" ... "exit"
    for line in clean_output(config).split("\n"):
        stripped = line.strip().lower()
        if summary.uptime_seconds is None and (m := _UPTIME_COMMENT_RE.match(line)):
            summary.uptime_seconds = parse_uptime(m.group(1))
            continue
        if m := _BLOCK_IF_RE.match(line):
            block_port = m.group(1).lower()
            continue
        if block_port is not None:
            if stripped == "exit":
                block_port = None
            elif stripped == "shutdown":
                summary.shutdown_ports.add(block_port)
            elif stripped in {"no poe", "poe disable", "no poe enable"}:
                summary.pse_shutdown_ports.add(block_port)
            continue
        if m := _BIND_RE.match(line):
            if not re.search(r"\bshutdown\b", m.group(4) or "", re.IGNORECASE):
                summary.ssid_bindings.setdefault(m.group(1).lower(), set()).add(m.group(2) or m.group(3))
            continue
        if m := _HIDE_RE.match(line):
            summary.hidden_ssids.add(m.group(1) or m.group(2))
        elif m := _SUITE_RE.match(line):
            summary.security_suites[m.group(1) or m.group(2)] = m.group(3)
        elif m := _PSE_OFF_RE.match(line):
            summary.pse_shutdown_ports.add(m.group(1).lower())
        elif m := _PORT_OFF_RE.match(line):
            summary.shutdown_ports.add(m.group(1).lower())
    return summary


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def resolve_ssid_name(value: str | None, names: Iterable[str]) -> str | None:
    """Match an SSID shown in a narrow table column to its full name.

    'show interface' can cut long SSID names short (sometimes with '...',
    '~' or '+'). Exact matches win; otherwise a unique prefix match is used.
    """
    if not value:
        return None
    names = list(names)
    if value in names:
        return value
    stem = re.sub(r"(\.\.\.|…|~|\+|\*)$", "", value).rstrip()
    if not stem:
        return value
    matches = [n for n in names if n.startswith(stem)]
    if len(matches) == 1:
        return matches[0]
    lowered = [n for n in names if n.lower() == value.lower()]
    return lowered[0] if len(lowered) == 1 else value


def radio_band(radio_profile: str | None, channel: int | None) -> str | None:
    """Best-effort band label for a radio."""
    if channel is not None:
        if 1 <= channel <= 14:
            return "2.4 GHz"
        if channel >= 32:
            return "5 GHz"
    if radio_profile:
        prof = radio_profile.lower().removeprefix("radio_")
        if re.match(r"^(ax|n|ac)?g", prof) or prof.startswith("ng") or prof.startswith("bg"):
            return "2.4 GHz"
        if re.match(r"^(ax|n|ac)?a", prof) or prof.startswith("na") or prof.startswith("ac"):
            return "5 GHz"
    return None


def to_dict(obj: Any) -> Any:
    """Dataclass -> dict (used for diagnostics)."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj
