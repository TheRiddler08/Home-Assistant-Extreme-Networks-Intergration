"""Pure parsing helpers for Aerohive HiveOS / Extreme IQ Engine CLI output.

This module must stay free of Home Assistant (and asyncssh) imports so it can
be unit tested in isolation. Every public parser is defensive: unknown or
malformed lines are skipped, never raised on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ipaddress
import re
from typing import Any

# --------------------------------------------------------------------------
# Generic text cleanup
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
# "--More--", "-- More --", " --More-- " optionally followed by the carriage
# return / space sequence many CLIs use to wipe the pager marker.
MORE_RE = re.compile(r"[ \t]*-+[ \t]*More[ \t]*-+[ \t]*(?:\r[ \t]*)*", re.IGNORECASE)
MORE_TAIL_RE = re.compile(r"-+[ \t]*More[ \t]*-+[ \t]*$", re.IGNORECASE)
CONFIRM_TAIL_RE = re.compile(
    r"(\((?:y/n|yes/no)\)|\[(?:y/n|yes/no)\]|<(?:y/n|yes/no)>|\b(?:y/n|yes/no)\b)"
    r"[^\n]{0,20}[:?]?\s*$",
    re.IGNORECASE,
)
# Generic HiveOS prompt: "AH-7c0680#". Anchored at the end of the buffer.
PROMPT_RE = re.compile(r"(?:^|\n)(?P<host>[A-Za-z0-9][A-Za-z0-9_\-\.]*)#[ \t]*$")

NONE_VALUES = {"", "-", "--", "n/a", "na", "none"}


def clean_output(text: str) -> str:
    """Strip ANSI codes, pager markers, backspaces and normalise newlines."""
    if not text:
        return ""
    text = ANSI_RE.sub("", text)
    text = MORE_RE.sub("", text)
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
    re.compile(r"^.*\b[Uu]nknown keyword\b.*$", re.MULTILINE),
    re.compile(r"^.*\bInvalid\b.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^.*\bIncomplete command\b.*$", re.MULTILINE | re.IGNORECASE),
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
    r"unknown keyword|invalid input|incomplete command|ambiguous command|^\s*ERROR\b|^\s*%",
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

SEPARATOR_RE = re.compile(r"^\s*-+(?:\s+-+)*\s*$")
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
            header = lines[idx - 1]
            spans = [_Span(m.start(), m.end()) for m in re.finditer(r"-+", line)]
        elif header_re is not None and header_re.search(line):
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if SEPARATOR_RE.match(nxt):
                idx += 1  # handled by separator branch next iteration
                continue
            header = line
            spans = _header_spans(line)
        if spans is None or header is None:
            idx += 1
            continue

        columns = [normalize_key(c) or f"col{i}" for i, c in enumerate(_split_by_spans(header, spans))]
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


def parse_key_values(text: str) -> dict[str, str]:
    """Parse 'Key: value' and 'Key=value; Key2=value2;' style output."""
    result: dict[str, str] = {}
    for line in clean_output(text).split("\n"):
        if not line.strip() or SEPARATOR_RE.match(line):
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
    """Parsed 'show version'."""

    version: str | None = None  # e.g. "IQ Engine 10.5r3 build-278650"
    firmware: str | None = None  # e.g. "10.5r3"
    build: str | None = None  # e.g. "278650"
    os_name: str | None = None  # e.g. "IQ Engine" / "HiveOS"
    platform: str | None = None  # e.g. "AP460C"
    bootloader: str | None = None
    uptime_seconds: int | None = None
    serial: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


def parse_show_version(text: str) -> VersionInfo:
    kv = parse_key_values(text)
    info = VersionInfo(raw=kv)
    info.version = kv.get("version") or kv.get("software_version") or kv.get("sw_version")
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
    for key in ("platform", "hardware_platform", "model", "product_name"):
        if kv.get(key):
            info.platform = kv[key].split()[0] if key == "platform" else kv[key]
            break
    for key, value in kv.items():
        if key.startswith("bootloader") and info.bootloader is None:
            info.bootloader = value
        elif "serial" in key and info.serial is None:
            info.serial = value
    info.uptime_seconds = parse_uptime(kv.get("uptime") or kv.get("system_uptime"))
    return info


# --------------------------------------------------------------------------
# show interface
# --------------------------------------------------------------------------

_IFACE_NAME_RE = re.compile(r"^(mgt|eth|wifi|red|agg|tunnel|usbnet|vlan|lte|br)\S*$", re.IGNORECASE)
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
    extra: dict[str, str] = field(default_factory=dict)

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


_IFACE_HEADER_RE = re.compile(r"^\s*Name\s+MAC\s+addr", re.IGNORECASE)


def _interface_from_cells(cells: dict[str, str]) -> Interface | None:
    name = (cells.get("name") or "").strip()
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
    known = {"name", "mac_addr", "mac", "mode", "state", "chan_width", "chan", "channel", "vlan", "radio", "hive", "ssid"}
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
    tables = [t for t in parse_tables(text, _IFACE_HEADER_RE) if "name" in t.columns]
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
    "rx_bytes": [r"^rx_bytes$", r"^rx_octets$", r"^received_bytes$", r"^rx_.*bytes$"],
    "tx_bytes": [r"^tx_bytes$", r"^tx_octets$", r"^transmitted_bytes$", r"^tx_.*bytes$"],
    "rx_packets": [r"^rx_packets$", r"^rx_pkts$", r"^rx_frames$", r"^rx_data_frames$"],
    "tx_packets": [r"^tx_packets$", r"^tx_pkts$", r"^tx_frames$", r"^tx_data_frames$"],
    "rx_errors": [r"^rx_errors$", r"^rx_errs?$", r"^rx_error"],
    "tx_errors": [r"^tx_errors$", r"^tx_errs?$", r"^tx_error"],
    "rx_dropped": [r"^rx_dropped$", r"^rx_drops?$", r"^rx_drop"],
    "tx_dropped": [r"^tx_dropped$", r"^tx_drops?$", r"^tx_drop"],
    # ethernet / addressing
    "speed": [r"^speed$", r"^link_speed$", r"^operational_speed$"],
    "duplex": [r"^duplex$", r"^operational_duplex$"],
    "admin_state": [r"^admin_state$", r"^admin_status$"],
    "operational_state": [r"^operational_state$", r"^oper_state$", r"^link_state$", r"^link$"],
    "mtu": [r"^mtu$"],
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
# Misc
# --------------------------------------------------------------------------


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
