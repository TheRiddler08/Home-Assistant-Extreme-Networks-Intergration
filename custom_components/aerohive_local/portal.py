"""Onboarding portal: a small web page on the OpenWrt router for the onboarding Wi-Fi.

How it works (no Home Assistant exposure to untrusted devices):

1. A device joins the onboarding Wi-Fi (e.g. homeOB). Any web page it opens is
   redirected (firewall DNAT, port 80 -> PORT) to the portal, so phones show
   their "sign in to network" screen.
2. The portal (a CGI script run by the router's own web server, uhttpd) asks
   for a name, the kind of device and which network it wants, and writes a
   request file to ``/tmp/hn_portal/req/<mac>`` on the router.
3. Home Assistant reads the requests over its SSH session to the router and
   shows them in the Setup tab (plus a notification and an event).
4. Approve: Home Assistant creates a per-device key and writes a page with the
   network name, key and QR code to ``/tmp/hn_portal/ok/<mac>.html``. The
   portal shows it to that device only (it must present the random token it got
   as a cookie when asking). Decline: the portal says so.

Onboarding devices only ever talk to the router; Home Assistant reaches out.
This module is pure Python (no Home Assistant).
"""

from __future__ import annotations

import html
import re
import shlex
from dataclasses import dataclass
from typing import Any

PORT = 8099
TMP = "/tmp/hn_portal"  # noqa: S108 - on the router (RAM; cleared by a reboot)
WWW = "/www/hn-portal"
CGI = f"{WWW}/cgi-bin/portal"
ARP = "/proc/net/arp"
MARK = "ERROR: OpenWrt did not accept the change"
KINDS = {"phone": "Phone or tablet", "computer": "Computer", "tv": "TV or streaming box", "speaker": "Speaker or smart-home device", "other": "Something else"}
MAX_PENDING = 20
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_TOKEN_RE = re.compile(r"^[a-z0-9]{16}$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]")

LIST_REQUESTS = f'for f in {TMP}/req/*; do [ -f "$f" ] && {{ echo "== ${{f##*/}}"; cat "$f"; }}; done; true'

CSS = (
    "body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f8;color:#1c1c1c}"
    "main{max-width:420px;margin:0 auto;padding:28px 18px}h1{font-size:22px;font-weight:600;margin:0 0 10px}"
    "p{line-height:1.45}label{display:block;font-weight:600;margin:16px 0 6px}"
    "input,select{width:100%;box-sizing:border-box;height:44px;padding:0 12px;border:1px solid #c8ced6;border-radius:10px;font:inherit;background:#fff}"
    "button{margin-top:22px;width:100%;height:46px;border:0;border-radius:23px;background:#0a84c1;color:#fff;font:inherit;font-weight:600}"
    ".key{font:600 24px ui-monospace,Menlo,monospace;letter-spacing:1px;background:#fff;border-radius:12px;padding:14px;text-align:center;word-break:break-all}"
    ".qr{text-align:center;margin:16px 0}.qr svg{width:min(240px,80vw);height:auto}.muted{color:#5e6875;font-size:14px}"
)


def _clean(value: str | None, limit: int = 40) -> str:
    return _SAFE_RE.sub("", value or "")[:limit].strip()


# --------------------------------------------------------------------------
# The CGI script
# --------------------------------------------------------------------------


def cgi_script(*, title: str, text: str, offer: list[tuple[str, str]]) -> str:
    """The portal script (busybox ash). ``offer``: [(wifi id, label)] devices may ask for.

    Everything that comes from the setup is HTML-escaped here; everything that
    comes from a device is reduced to letters, digits, space, '.', '_' and '-'
    by the script before it's stored.
    """
    title_h = html.escape(title.replace("\n", " ").strip() or "Welcome")
    text_h = html.escape(text.strip()).replace("\r", "").replace("\n", "<br>")
    offer_ids = " ".join(i for i, _ in offer if re.match(r"^[a-z][a-z0-9_]{0,15}$", i))
    options = "".join(f'<option value="{html.escape(i)}">{html.escape(label)}</option>' for i, label in offer)
    kinds = "".join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in KINDS.items())
    kind_ids = "|".join(KINDS)
    lines = [
        "#!/bin/sh",
        "# Onboarding portal, written by Home Assistant (aerohive_local). Replaced on every apply.",
        f"D={TMP}",
        f"TITLE={shlex.quote(title_h)}",
        f"CSS={shlex.quote(CSS)}",
        f"OFFER={shlex.quote(offer_ids)}",
        "HEAD=; EXTRA=",
        "umask 077",
        'mkdir -p "$D/req" "$D/ok" "$D/no"',
        f"mac=$(awk -v ip=\"$REMOTE_ADDR\" '$1 == ip {{ print tolower($4) }}' {ARP} | head -n 1)",
        'case "$mac" in [0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]) ;; *) mac= ;; esac',
        "id=$(printf '%s' \"$mac\" | tr -d ':')",
        "tok=$(printf '%s' \"$HTTP_COOKIE\" | sed -n 's/.*hn_tok=\\([a-z0-9]\\{16\\}\\).*/\\1/p')",
        "clean() { sed 's/+/ /g; s/%[0-9A-Fa-f][0-9A-Fa-f]/ /g' | tr -cd 'A-Za-z0-9 ._-' | cut -c1-40; }",
        "page() {",
        "  printf 'Content-Type: text/html; charset=utf-8\\r\\nCache-Control: no-store\\r\\n'",
        "  [ -n \"$EXTRA\" ] && printf '%s\\r\\n' \"$EXTRA\"",
        "  printf '\\r\\n<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">%s<title>%s</title><style>%s</style></head><body><main>' \"$HEAD\" \"$TITLE\" \"$CSS\"",
        "  cat",
        "  printf '</main></body></html>\\n'",
        "}",
        'if [ -z "$id" ]; then',
        "  page <<'HNEOF'",
        f"<h1>{title_h}</h1><p>Your device couldn't be recognised. Turn Wi-Fi off and on, then open this page again.</p>",
        "HNEOF",
        "  exit 0",
        "fi",
        'if [ -f "$D/ok/$id.html" ]; then',
        '  if [ -n "$tok" ] && [ "$tok" = "$(cat "$D/ok/$id.tok" 2>/dev/null)" ]; then page < "$D/ok/$id.html"; exit 0; fi',
        "  page <<'HNEOF'",
        f"<h1>{title_h}</h1><p>Your request was approved. Open this page in the browser you used to ask, to see the details.</p>",
        "HNEOF",
        "  exit 0",
        "fi",
        'if [ -f "$D/no/$id" ]; then',
        "  page <<'HNEOF'",
        f"<h1>{title_h}</h1><p>Sorry, this request was declined.</p>",
        "HNEOF",
        "  exit 0",
        "fi",
        'if [ "$REQUEST_METHOD" = POST ] && [ ! -f "$D/req/$id" ]; then',
        '  len=${CONTENT_LENGTH:-0}; case "$len" in ""|*[!0-9]*) len=0 ;; esac; [ "$len" -gt 2048 ] && len=2048',
        '  body=$(head -c "$len")',
        "  field() { printf '%s\\n' \"$body\" | tr '&' '\\n' | sed -n \"s/^$1=//p\" | head -n 1 | clean; }",
        "  name=$(field name); kind=$(field kind); wants=$(field wants)",
        f'  case "$kind" in {kind_ids}) ;; *) kind=other ;; esac',
        '  case " $OFFER " in *" $wants "*) ;; *) wants= ;; esac',
        f'  if [ -n "$name" ] && [ "$(ls "$D/req" | wc -l)" -lt {MAX_PENDING} ]; then',
        "    tok=$(tr -dc 'a-z0-9' < /dev/urandom | head -c 16)",
        "    printf 'mac=%s\\nip=%s\\nname=%s\\nkind=%s\\nwants=%s\\ntok=%s\\ntime=%s\\n' \"$mac\" \"$REMOTE_ADDR\" \"$name\" \"$kind\" \"$wants\" \"$tok\" \"$(date +%s)\" > \"$D/req/$id.tmp\" && mv \"$D/req/$id.tmp\" \"$D/req/$id\"",
        '    EXTRA="Set-Cookie: hn_tok=$tok; Path=/; HttpOnly; SameSite=Lax"',
        "  fi",
        "fi",
        'if [ -f "$D/req/$id" ]; then',
        "  HEAD='<meta http-equiv=\"refresh\" content=\"5\">'",
        "  page <<'HNEOF'",
        f"<h1>{title_h}</h1><p>Thanks! Your request is waiting for approval.</p><p class=\"muted\">Keep this page open: it updates by itself.</p>",
        "HNEOF",
        "  exit 0",
        "fi",
        "page <<'HNEOF'",
        f"<h1>{title_h}</h1>" + (f"<p>{text_h}</p>" if text_h else "")
        + '<form method="post" action="/cgi-bin/portal">'
        + '<label for="name">Name for this device</label><input id="name" name="name" maxlength="40" required placeholder="e.g. Living room TV">'
        + f'<label for="kind">What is it?</label><select id="kind" name="kind">{kinds}</select>'
        + (f'<label for="wants">Which network?</label><select id="wants" name="wants">{options}</select>' if options else "")
        + "<button type=\"submit\">Ask for access</button></form>",
        "HNEOF",
    ]
    return "\n".join(lines) + "\n"


def approved_page(*, title: str, name: str, ssid: str, key: str, qr_svg: str | None, expires_text: str | None) -> str:
    """What the portal shows the approved device (written to the router by Home Assistant)."""
    parts = [
        f"<h1>{html.escape(title or 'Welcome')}</h1>",
        f"<p>{html.escape(name)} may now join <strong>{html.escape(ssid)}</strong> with this password:</p>",
        f'<div class="key">{html.escape(key)}</div>',
    ]
    if qr_svg:
        parts.append(f'<div class="qr">{qr_svg}</div><p class="muted">Or scan this code with the device\'s camera.</p>')
    if expires_text:
        parts.append(f'<p class="muted">{html.escape(expires_text)}</p>')
    parts.append('<p class="muted">Then forget this onboarding network on the device.</p>')
    return "".join(parts)


# --------------------------------------------------------------------------
# Commands for the router
# --------------------------------------------------------------------------


def _write_file(path: str, content: str, *, mode: str | None = None, piece: int = 900) -> list[str]:
    """Write a file with printf, then move it into place.

    Every command is a single line (an interactive SSH shell takes one line at a
    time): the content is sent line by line, long lines in pieces, and the
    newlines are added by printf's format, never sent inside quotes.
    """
    tmp = f"{path}.new"
    cmds = [f": > {tmp}"]
    lines = content.split("\n")
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        if last and not line:
            break  # the content ended with a newline
        pieces = [line[i:i + piece] for i in range(0, len(line), piece)] or [""]
        for number, part in enumerate(pieces):
            newline = number == len(pieces) - 1 and not last
            cmds.append(f"printf '{'%s\\n' if newline else '%s'}' {shlex.quote(part)} >> {tmp}")
    if mode:
        cmds.append(f"chmod {mode} {tmp}")
    cmds.append(f"mv {tmp} {path}")
    return cmds


def _uhttpd_cleanup() -> str:
    return "for s in $(uci -q show uhttpd | sed -n 's/^uhttpd\\.\\(hn_[A-Za-z0-9_]*\\)=.*/\\1/p'); do uci delete uhttpd.$s; done"


UHTTPD_RELOAD = "(trap '' HUP INT TERM; sleep 1; /etc/init.d/uhttpd reload) </dev/null >/dev/null 2>&1 &"
UHTTPD_CHECK = "[ -f /etc/config/uhttpd ] && echo HN_UHTTPD_OK"


def install_lines(*, address: str, title: str, text: str, offer: list[tuple[str, str]]) -> list[str]:
    """Put the portal on the router and have uhttpd serve it on ``address``:PORT only."""
    cmds = [f"mkdir -p {WWW}/cgi-bin"]
    cmds += _write_file(CGI, cgi_script(title=title, text=text, offer=offer), mode="755")
    cmds += [
        _uhttpd_cleanup(),
        "uci set uhttpd.hn_portal=uhttpd",
        f"uci add_list uhttpd.hn_portal.listen_http={shlex.quote(f'{address}:{PORT}')}",
        f"uci set uhttpd.hn_portal.home={WWW}",
        "uci set uhttpd.hn_portal.cgi_prefix=/cgi-bin",
        "uci set uhttpd.hn_portal.error_page=/cgi-bin/portal",
        "uci set uhttpd.hn_portal.index_page=cgi-bin/portal",
        "uci set uhttpd.hn_portal.no_dirlists=1",
        "uci set uhttpd.hn_portal.rfc1918_filter=0",
        "uci set uhttpd.hn_portal.max_requests=5",
        "uci set uhttpd.hn_portal.script_timeout=10",
        "uci set uhttpd.hn_portal.network_timeout=10",
        "uci commit uhttpd",
    ]
    return cmds


def remove_lines() -> list[str]:
    return [_uhttpd_cleanup(), "uci commit uhttpd", f"rm -rf {WWW}"]


def approve_lines(request_id: str, token: str, page: str) -> list[str]:
    if not _ID_RE.match(request_id) or not _TOKEN_RE.match(token):
        raise ValueError("invalid request")
    cmds = [f"mkdir -p {TMP}/ok"]
    cmds += _write_file(f"{TMP}/ok/{request_id}.html", page)
    cmds += _write_file(f"{TMP}/ok/{request_id}.tok", token)
    cmds.append(f"rm -f {TMP}/req/{request_id} {TMP}/no/{request_id}")
    return cmds


def decline_lines(request_id: str) -> list[str]:
    if not _ID_RE.match(request_id):
        raise ValueError("invalid request")
    return [f"mkdir -p {TMP}/no && : > {TMP}/no/{request_id} && rm -f {TMP}/req/{request_id}"]


def forget_lines(request_id: str) -> list[str]:
    """Remove everything about a request from the router."""
    if not _ID_RE.match(request_id):
        raise ValueError("invalid request")
    return [f"rm -f {TMP}/req/{request_id} {TMP}/no/{request_id} {TMP}/ok/{request_id}.html {TMP}/ok/{request_id}.tok"]


# --------------------------------------------------------------------------
# Reading requests
# --------------------------------------------------------------------------


@dataclass
class Request:
    id: str
    mac: str
    ip: str | None
    name: str
    kind: str
    wants: str | None
    token: str
    time: int | None


def parse_requests(text: str) -> list[Request]:
    """Parse LIST_REQUESTS output; anything malformed is skipped."""
    result: list[Request] = []
    for block in re.split(r"^== ", text or "", flags=re.MULTILINE)[1:]:
        head, _, body = block.partition("\n")
        rid = head.strip()
        if not _ID_RE.match(rid):
            continue
        fields: dict[str, str] = {}
        for line in body.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        mac = fields.get("mac", "").lower()
        token = fields.get("tok", "")
        if not _MAC_RE.match(mac) or mac.replace(":", "") != rid or not _TOKEN_RE.match(token):
            continue
        name = _clean(fields.get("name"))
        if not name:
            continue
        kind = fields.get("kind") if fields.get("kind") in KINDS else "other"
        ip = fields.get("ip") if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", fields.get("ip", "")) else None
        wants = fields.get("wants") if re.match(r"^[a-z][a-z0-9_]{0,15}$", fields.get("wants", "")) else None
        stamp = fields.get("time", "")
        result.append(Request(rid, mac, ip, name, kind, wants, token, int(stamp) if stamp.isdigit() else None))
    return result


def firewall_lines(zone: str, vid: int, address: str) -> list[str]:
    """Let the onboarding network reach the portal, and send its web traffic there."""
    rule, redirect = f"hn_portal{vid}_in", f"hn_portal{vid}_dnat"
    return [
        f"uci set firewall.{rule}=rule",
        f"uci set firewall.{rule}.name='hn{vid} portal'",
        f"uci set firewall.{rule}.src={zone}",
        f"uci set firewall.{rule}.proto=tcp",
        f"uci set firewall.{rule}.dest_port={PORT}",
        f"uci set firewall.{rule}.target=ACCEPT",
        f"uci set firewall.{redirect}=redirect",
        f"uci set firewall.{redirect}.name='hn{vid} portal redirect'",
        f"uci set firewall.{redirect}.src={zone}",
        f"uci set firewall.{redirect}.proto=tcp",
        f"uci set firewall.{redirect}.src_dport=80",
        f"uci set firewall.{redirect}.dest_ip={shlex.quote(address)}",
        f"uci set firewall.{redirect}.dest_port={PORT}",
        f"uci set firewall.{redirect}.target=DNAT",
    ]


def as_dict(request: Request) -> dict[str, Any]:
    """For the panel (no token)."""
    return {"id": request.id, "mac": request.mac, "ip": request.ip, "name": request.name, "kind": request.kind, "wants": request.wants, "time": request.time}
