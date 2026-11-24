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
MAX_PENDING = 20
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_TOKEN_RE = re.compile(r"^[a-z0-9]{16}$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]")

LIST_REQUESTS = f'for f in {TMP}/req/*; do [ -f "$f" ] && {{ echo "== ${{f##*/}}"; cat "$f"; }}; done; true'

DEFAULT_KINDS = ["Phone or tablet", "Computer", "TV or streaming box", "Speaker or smart-home device", "Something else"]
THEME_DEFAULTS = {"accent": "#0a84c1", "background": "#f4f6f8", "text": "#1c1c1c", "card": "#ffffff"}
TEXT_DEFAULTS = {
    "title": "Welcome",
    "text": "Tell us what this device is, and it gets its own Wi-Fi password once approved.",
    "button": "Ask for access",
    "waiting_text": "Thanks! Your request is waiting for approval.",
    "approved_text": "{device} may now join {network} with this password:",
    "declined_text": "Sorry, this request was declined.",
    "footer": "",
}
MAX_TEMPLATE = 30_000
MAX_LOGO = 96_000
LOGO_RE = re.compile(r"^data:image/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/]+=*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PLACEHOLDERS = ("{{title}}", "{{style}}", "{{logo}}", "{{content}}", "{{footer}}")

# Your own page starts from this. {{content}} (required) becomes the form, the
# waiting message, or the password and QR code; the others are optional.
DEFAULT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title>
<style>{{style}}</style>
</head>
<body>
<main>
{{logo}}
{{content}}
{{footer}}
</main>
</body>
</html>
"""


def _clean(value: str | None, limit: int = 40) -> str:
    return _SAFE_RE.sub("", value or "")[:limit].strip()


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:16].strip("_")


@dataclass
class Look:
    """How the portal looks and what it says (from the setup's onboarding settings)."""

    title: str
    text: str
    button: str
    waiting_text: str
    approved_text: str
    declined_text: str
    footer: str
    logo: str | None
    colors: dict[str, str]
    kinds: list[tuple[str, str]]  # (id, label)
    ask_network: bool
    template: str  # the page around {{content}}
    max_per_hour: int = 10


def kind_choices(labels: list[str] | None) -> list[tuple[str, str]]:
    """Device kinds as (id, label); ids are made from the labels (unique, safe in the script)."""
    result: list[tuple[str, str]] = []
    for label in [str(x).strip()[:40] for x in labels or [] if str(x).strip()] or DEFAULT_KINDS:
        base = _slug(label) or "kind"
        kid, n = base, 2
        while kid in {k for k, _ in result}:
            kid, n = f"{base[:13]}_{n}", n + 1
        result.append((kid, label))
    return result[:12]


def look_from(settings: dict[str, Any]) -> Look:
    def text(key: str) -> str:
        value = settings.get(key)
        return TEXT_DEFAULTS[key] if value is None else str(value)

    raw = settings.get("colors") if isinstance(settings.get("colors"), dict) else {}
    colors = {k: str(raw[k]) if COLOR_RE.match(str(raw.get(k) or "")) else default for k, default in THEME_DEFAULTS.items()}
    logo = settings.get("logo")
    template = settings.get("custom_html") if settings.get("use_custom") and settings.get("custom_html") else DEFAULT_TEMPLATE
    return Look(
        title=text("title").replace("\n", " ").strip() or "Welcome",
        text=text("text"),
        button=text("button").strip() or TEXT_DEFAULTS["button"],
        waiting_text=text("waiting_text"),
        approved_text=text("approved_text"),
        declined_text=text("declined_text"),
        footer=text("footer"),
        logo=logo if isinstance(logo, str) and LOGO_RE.match(logo) and len(logo) <= MAX_LOGO else None,
        colors=colors,
        kinds=kind_choices(settings.get("kinds")),
        ask_network=settings.get("ask_network", True) is not False,
        template=str(template),
        max_per_hour=settings["max_per_hour"] if isinstance(settings.get("max_per_hour"), int) and 1 <= settings["max_per_hour"] <= 100 else 10,
    )


def validate_look(settings: dict[str, Any]) -> list[str]:
    """Problems with the portal's look settings (empty: fine)."""
    errors = []
    for key, limit in (("title", 60), ("text", 400), ("button", 40), ("waiting_text", 300), ("approved_text", 300), ("declined_text", 300), ("footer", 400)):
        if len(str(settings.get(key) or "")) > limit:
            errors.append(f"Onboarding portal: the {key.replace('_', ' ')} can have {limit} characters")
    logo = settings.get("logo")
    if logo and (not isinstance(logo, str) or not LOGO_RE.match(logo)):
        errors.append("Onboarding portal: the logo must be a PNG, JPEG, GIF, WebP or SVG image")
    elif logo and len(logo) > MAX_LOGO:
        errors.append("Onboarding portal: the logo is too big (about 70 KB at most)")
    colors = settings.get("colors") or {}
    if not isinstance(colors, dict) or any(v and not COLOR_RE.match(str(v)) for v in colors.values()):
        errors.append("Onboarding portal: colours must look like #0a84c1")
    kinds = settings.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or len(kinds) > 12 or any(len(str(k)) > 40 for k in kinds)):
        errors.append("Onboarding portal: up to 12 device kinds of 40 characters each")
    if settings.get("use_custom"):
        page = str(settings.get("custom_html") or "")
        if page.count("{{content}}") != 1:
            errors.append("Onboarding portal: your own page needs {{content}} exactly once")
        if len(page) > MAX_TEMPLATE:
            errors.append("Onboarding portal: your own page can have 30,000 characters")
    return errors


def css(look: Look) -> str:
    c = look.colors
    return (
        f":root{{--accent:{c['accent']};--bg:{c['background']};--text:{c['text']};--card:{c['card']}}}"
        "body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}"
        "main{max-width:420px;margin:0 auto;padding:28px 18px}h1{font-size:22px;font-weight:600;margin:0 0 10px}"
        "p{line-height:1.45}label{display:block;font-weight:600;margin:16px 0 6px}"
        "input,select{width:100%;box-sizing:border-box;height:44px;padding:0 12px;border:1px solid rgba(127,127,127,.45);border-radius:10px;font:inherit;background:var(--card);color:inherit}"
        "button{margin-top:22px;width:100%;height:46px;border:0;border-radius:23px;background:var(--accent);color:#fff;font:inherit;font-weight:600}"
        ".key{font:600 24px ui-monospace,Menlo,monospace;letter-spacing:1px;background:var(--card);border-radius:12px;padding:14px;text-align:center;word-break:break-all}"
        ".qr{text-align:center;margin:16px 0}.qr svg{width:min(240px,80vw);height:auto;border-radius:8px}"
        ".muted{opacity:.72;font-size:14px}.logo{display:block;max-width:180px;max-height:80px;margin:0 0 18px}"
        ".footer{margin-top:28px;font-size:13px;opacity:.7}"
    )


def _para(text: str) -> str:
    return html.escape(text.strip()).replace("\r", "").replace("\n", "<br>")


def frame(look: Look) -> tuple[str, str]:
    """The page before and after {{content}}."""
    logo = f'<img class="logo" src="{look.logo}" alt="">' if look.logo else ""
    footer = f'<div class="footer">{_para(look.footer)}</div>' if look.footer.strip() else ""
    page = (
        look.template.replace("{{title}}", html.escape(look.title))
        .replace("{{style}}", css(look))
        .replace("{{logo}}", logo)
        .replace("{{footer}}", footer)
    )
    top, sep, bottom = page.partition("{{content}}")
    if not sep:  # not validated: fall back to the default page
        return frame(Look(**{**look.__dict__, "template": DEFAULT_TEMPLATE}))
    return top, bottom


def _h1(look: Look) -> str:
    return f"<h1>{html.escape(look.title)}</h1>"


def form_html(look: Look, offer: list[tuple[str, str]]) -> str:
    kinds = "".join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in look.kinds)
    options = "".join(f'<option value="{html.escape(i)}">{html.escape(label)}</option>' for i, label in offer)
    text = _para(look.text)
    return (
        _h1(look) + (f"<p>{text}</p>" if text else "")
        + '<form method="post" action="/cgi-bin/portal">'
        + '<label for="name">Name for this device</label><input id="name" name="name" maxlength="40" required placeholder="e.g. Living room TV">'
        + f'<label for="kind">What is it?</label><select id="kind" name="kind">{kinds}</select>'
        + (f'<label for="wants">Which network?</label><select id="wants" name="wants">{options}</select>' if options and look.ask_network else "")
        + f'<button type="submit">{html.escape(look.button)}</button></form>'
    )


def waiting_html(look: Look) -> str:
    return (
        '<meta http-equiv="refresh" content="5">' + _h1(look) + f"<p>{_para(look.waiting_text)}</p>"
        + '<p class="muted">Keep this page open: it updates by itself.</p>'
    )


def declined_html(look: Look) -> str:
    return _h1(look) + f"<p>{_para(look.declined_text)}</p>"


def unknown_html(look: Look) -> str:
    return _h1(look) + "<p>Your device couldn't be recognised. Turn Wi-Fi off and on, then open this page again.</p>"


def busy_html(look: Look) -> str:
    return _h1(look) + "<p>Many devices have asked in the last hour. Please try again a bit later.</p>"


def other_browser_html(look: Look) -> str:
    return _h1(look) + "<p>Your request was approved. Open this page in the browser you used to ask, to see the details.</p>"


def approved_page(*, look: Look, name: str, ssid: str, key: str, qr_svg: str | None, expires_text: str | None) -> str:
    """What the portal shows the approved device (written to the router by Home Assistant)."""
    intro = _para(look.approved_text).replace("{device}", html.escape(name)).replace("{network}", f"<strong>{html.escape(ssid)}</strong>")
    parts = [_h1(look), f"<p>{intro}</p>", f'<div class="key">{html.escape(key)}</div>']
    if qr_svg:
        parts.append(f'<div class="qr">{qr_svg}</div><p class="muted">Or scan this code with the device\'s camera.</p>')
    if expires_text:
        parts.append(f'<p class="muted">{html.escape(expires_text)}</p>')
    parts.append('<p class="muted">Then forget this onboarding network on the device.</p>')
    return "".join(parts).replace("\n", " ")


def preview(look: Look, offer: list[tuple[str, str]], page: str, *, qr_svg: str | None = None) -> str:
    """A whole page as a device would see it (for the panel's preview)."""
    top, bottom = frame(look)
    body = {
        "form": lambda: form_html(look, offer),
        "waiting": lambda: waiting_html(look).replace('<meta http-equiv="refresh" content="5">', ""),
        "declined": lambda: declined_html(look),
        "approved": lambda: approved_page(look=look, name="Living room TV", ssid=offer[0][1] if offer else "home",
                                          key="Kx7mPq2vR9tw", qr_svg=qr_svg, expires_text=None),
    }.get(page, lambda: form_html(look, offer))()
    return top + body + bottom


# --------------------------------------------------------------------------
# The CGI script
# --------------------------------------------------------------------------


def cgi_script(look: Look, offer: list[tuple[str, str]]) -> str:
    """The portal script (busybox ash). ``offer``: [(wifi id, label)] devices may ask for.

    Everything that comes from the setup is HTML-escaped here (your own page is
    used as you wrote it); everything that comes from a device is reduced to
    letters, digits, space, '.', '_' and '-' by the script before it's stored.
    """
    offer_ids = " ".join(i for i, _ in offer if re.match(r"^[a-z][a-z0-9_]{0,15}$", i)) if look.ask_network else ""
    kind_ids = "|".join(k for k, _ in look.kinds)
    fallback_kind = look.kinds[-1][0]
    top, bottom = frame(look)

    def body(content: str) -> list[str]:
        # Contents are single lines, so the here-document can't be ended early.
        return ["  page <<'HNEOF'", content.replace("\n", " "), "HNEOF"]

    lines = [
        "#!/bin/sh",
        "# Onboarding portal, written by Home Assistant (aerohive_local). Replaced on every apply.",
        f"D={TMP}",
        f"TOP={shlex.quote(top)}",
        f"BOTTOM={shlex.quote(bottom)}",
        f"OFFER={shlex.quote(offer_ids)}",
        "EXTRA=; REFRESH=",
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
        "  [ -n \"$REFRESH\" ] && printf 'Refresh: %s\\r\\n' \"$REFRESH\"",
        "  printf '\\r\\n%s' \"$TOP\"",
        "  cat",
        "  printf '%s' \"$BOTTOM\"",
        "}",
        'if [ -z "$id" ]; then',
        *body(unknown_html(look)),
        "  exit 0",
        "fi",
        'if [ -f "$D/ok/$id.html" ]; then',
        '  if [ -n "$tok" ] && [ "$tok" = "$(cat "$D/ok/$id.tok" 2>/dev/null)" ]; then page < "$D/ok/$id.html"; exit 0; fi',
        *body(other_browser_html(look)),
        "  exit 0",
        "fi",
        'if [ -f "$D/no/$id" ]; then',
        *body(declined_html(look)),
        "  exit 0",
        "fi",
        'if [ "$REQUEST_METHOD" = POST ] && [ ! -f "$D/req/$id" ]; then',
        '  len=${CONTENT_LENGTH:-0}; case "$len" in ""|*[!0-9]*) len=0 ;; esac; [ "$len" -gt 2048 ] && len=2048',
        '  body=$(head -c "$len")',
        "  field() { printf '%s\\n' \"$body\" | tr '&' '\\n' | sed -n \"s/^$1=//p\" | head -n 1 | clean; }",
        "  name=$(field name); kind=$(field kind); wants=$(field wants)",
        f'  case "$kind" in {kind_ids}) ;; *) kind={fallback_kind} ;; esac',
        '  case " $OFFER " in *" $wants "*) ;; *) wants= ;; esac',
        f'  if [ -n "$name" ] && [ "$(ls "$D/req" | wc -l)" -lt {MAX_PENDING} ]; then',
        "    now=$(date +%s); touch \"$D/hits\"",
        "    awk -v t=$((now - 3600)) '$1 > t' \"$D/hits\" > \"$D/hits.new\" && mv \"$D/hits.new\" \"$D/hits\"",
        f'    if [ "$(wc -l < "$D/hits")" -ge {look.max_per_hour} ]; then',
        *body(busy_html(look)),  # the end marker must stay at the start of its line
        "      exit 0",
        "    fi",
        '    echo "$now" >> "$D/hits"',
        "    tok=$(tr -dc 'a-z0-9' < /dev/urandom | head -c 16)",
        "    printf 'mac=%s\\nip=%s\\nname=%s\\nkind=%s\\nwants=%s\\ntok=%s\\ntime=%s\\n' \"$mac\" \"$REMOTE_ADDR\" \"$name\" \"$kind\" \"$wants\" \"$tok\" \"$(date +%s)\" > \"$D/req/$id.tmp\" && mv \"$D/req/$id.tmp\" \"$D/req/$id\"",
        '    EXTRA="Set-Cookie: hn_tok=$tok; Path=/; HttpOnly; SameSite=Lax"',
        "  fi",
        "fi",
        'if [ -f "$D/req/$id" ]; then',
        "  REFRESH=5",
        *body(waiting_html(look)),
        "  exit 0",
        "fi",
        *[line.removeprefix("  ") for line in body(form_html(look, offer))],
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Commands for the router
# --------------------------------------------------------------------------


def _pieces(text: str, limit: int) -> list[str]:
    """Split ``text`` so each piece, shell-quoted, is at most ``limit`` characters."""
    if len(shlex.quote(text)) <= limit:
        return [text]
    middle = len(text) // 2
    return _pieces(text[:middle], limit) + _pieces(text[middle:], limit)


def _write_file(path: str, content: str, *, mode: str | None = None, piece: int = 600) -> list[str]:
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
        pieces = _pieces(line, piece) if line else [""]
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


def install_lines(*, address: str, look: Look, offer: list[tuple[str, str]]) -> list[str]:
    """Put the portal on the router and have uhttpd serve it on ``address``:PORT only."""
    cmds = [f"mkdir -p {WWW}/cgi-bin"]
    cmds += _write_file(CGI, cgi_script(look, offer), mode="755")
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
        kind = fields.get("kind") if re.match(r"^[a-z0-9_]{1,16}$", fields.get("kind", "")) else "other"
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
