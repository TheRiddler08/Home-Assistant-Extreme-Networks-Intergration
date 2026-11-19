"""QR codes for joining a Wi-Fi network (the ``WIFI:`` format phones understand).

A QR code holds the key itself, so it's treated like the key: only built on
request for an administrator, never stored or logged.
"""

from __future__ import annotations

import segno

_SPECIAL = str.maketrans({c: "\\" + c for c in r'\;,:"'})


def wifi_payload(ssid: str, key: str | None, security: str, hidden: bool = False) -> str:
    """``WIFI:T:WPA;S:<ssid>;P:<key>;;`` with the special characters escaped."""
    kind = "nopass" if security == "open" or not key else "SAE" if security == "wpa3" else "WPA"
    parts = [f"T:{kind}", f"S:{ssid.translate(_SPECIAL)}"]
    if kind != "nopass":
        parts.append(f"P:{(key or '').translate(_SPECIAL)}")
    if hidden:
        parts.append("H:true")
    return "WIFI:" + ";".join(parts) + ";;"


def svg(data: str) -> str:
    """An inline SVG (black on white, as scanners expect)."""
    return segno.make(data, error="m", micro=False).svg_inline(scale=5, border=3, dark="#000", light="#fff", omitsize=True)
