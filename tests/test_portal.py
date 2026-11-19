"""Onboarding portal: the router-side CGI script (run in a real POSIX shell) and request parsing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local import portal, qr

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

MAC = "02:11:22:33:44:55"
RID = "021122334455"


@pytest.fixture
def router(tmp_path: Path):
    """Run portal commands and the CGI with the router's paths moved into tmp_path."""
    arp = tmp_path / "arp"
    arp.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        f"10.8.10.60       0x1         0x2         {MAC.upper()}     *        br-hn80\n"
    )
    look = portal.look_from({"title": 'Welcome <home> & "co"', "text": "Line one\nHNEOF\n<b>bold</b>", "kinds": ["Phone", "TV", "Other"]})
    script = portal.cgi_script(look, [("homeiso", "homeISO"), ("home", "home")])

    def subst(text: str) -> str:
        return text.replace(portal.TMP, str(tmp_path / "hn")).replace(portal.ARP, str(arp))

    cgi = tmp_path / "portal.cgi"
    cgi.write_text(subst(script))

    def request(ip: str = "10.8.10.60", body: str | None = None, cookie: str = "") -> tuple[dict[str, str], str]:
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "REMOTE_ADDR": ip, "REQUEST_METHOD": "POST" if body is not None else "GET",
               "CONTENT_LENGTH": str(len(body or "")), "HTTP_COOKIE": cookie}
        raw = subprocess.run(["sh", str(cgi)], input=(body or "").encode(), capture_output=True, env=env, timeout=20, check=True).stdout
        out = raw.decode()  # bytes: keep the \r\n of the HTTP headers
        head, _, page = out.partition("\r\n\r\n")
        headers = dict(line.split(": ", 1) for line in head.split("\r\n") if ": " in line)
        return headers, page

    def sh(command: str) -> str:
        return subprocess.run(["sh", "-c", subst(command)], capture_output=True, text=True, timeout=20, check=True).stdout

    request.sh = sh  # type: ignore[attr-defined]
    request.dir = tmp_path / "hn"  # type: ignore[attr-defined]
    return request


@needs_sh
def test_portal_request_approve_flow(router) -> None:
    headers, page = router()
    assert headers["Content-Type"].startswith("text/html")
    assert '<option value="homeiso">homeISO</option>' in page and 'name="kind"' in page
    assert "Welcome &lt;home&gt; &amp; &quot;co&quot;" in page
    assert "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page and "Line one<br>HNEOF<br>" in page

    # Unknown device (not in the ARP table).
    _, page = router(ip="10.8.10.99")
    assert "couldn't be recognised" in page

    # Ask for access: name and choice are cleaned, a token comes back as a cookie.
    headers, page = router(body="name=Living+room+TV%3Cscript%3E%22%3B&kind=tv&wants=homeiso&x=1")
    token = headers["Set-Cookie"].split("hn_tok=")[1].split(";")[0]
    assert len(token) == 16 and "waiting for approval" in page and 'http-equiv="refresh"' in page
    stored = (router.dir / "req" / RID).read_text()
    assert "name=Living room TV script" in stored and "kind=tv" in stored and "wants=homeiso" in stored

    # Home Assistant reads it over SSH.
    requests = portal.parse_requests(router.sh(portal.LIST_REQUESTS))
    assert [(r.id, r.mac, r.name, r.kind, r.wants, r.token) for r in requests] == [(RID, MAC, "Living room TV script", "tv", "homeiso", token)]

    # Asking again doesn't replace the request; it keeps waiting.
    headers, page = router(body="name=Other&kind=phone&wants=home")
    assert "Set-Cookie" not in headers and "waiting for approval" in page

    # Approved: only the browser with the token sees the key and QR code.
    page_html = portal.approved_page(look=portal.look_from({}), name="Living room TV", ssid="homeISO", key="Kx7mPq2vR9tw",
                                     qr_svg=qr.svg(qr.wifi_payload("homeISO", "Kx7mPq2vR9tw", "ppsk")), expires_text=None)
    router.sh(" && ".join(portal.approve_lines(RID, token, page_html)))
    assert not (router.dir / "req" / RID).exists()
    _, page = router(cookie=f"other=1; hn_tok={token}")
    assert "Kx7mPq2vR9tw" in page and "<svg" in page
    _, page = router(cookie="hn_tok=aaaaaaaaaaaaaaaa")
    assert "Kx7mPq2vR9tw" not in page and "browser you used" in page


@needs_sh
def test_portal_decline_and_bad_choice(router) -> None:
    router(body="name=Laptop&kind=hacker&wants=mgmt")
    stored = (router.dir / "req" / RID).read_text()
    assert "kind=other" in stored  # not one of the kinds: the last one and "wants=\n" in stored  # not offered: nothing chosen
    router.sh(portal.decline_lines(RID)[0])
    _, page = router()
    assert "declined" in page
    router.sh(portal.forget_lines(RID)[0])
    _, page = router()
    assert "Ask for access" in page


def test_parse_requests_rejects_junk() -> None:
    text = (
        "== 021122334455\nmac=02:11:22:33:44:55\nname=TV\nkind=tv\nwants=homeiso\ntok=abcdefghij012345\ntime=1700000000\nip=10.8.10.60\n"
        "== 021122334466\nmac=02:11:22:33:44:55\nname=Mismatch\ntok=abcdefghij012345\n"  # MAC doesn't match the file name
        "== ../etc\nmac=x\n"
        "== 0211223344aa\nmac=02:11:22:33:44:aa\nname=<script>\ntok=short\n"
    )
    requests = portal.parse_requests(text)
    assert [r.id for r in requests] == ["021122334455"]
    assert requests[0].time == 1700000000 and requests[0].ip == "10.8.10.60"
    with pytest.raises(ValueError):
        portal.approve_lines("../../etc", "abcdefghij012345", "x")
    with pytest.raises(ValueError):
        portal.decline_lines("rm -rf /")


def test_portal_in_the_network_setup() -> None:
    plan = n.normalize(n.template())
    plan["devices"]["router"] = "r"
    plan["onboarding"]["enabled"] = True
    assert n.validate(plan) == []
    assert n.portal_address(plan) == "10.8.10.1"
    out = n.openwrt_lines(plan, lambda ref: "password123", is_router=True, radios={"radio0": "2.4 GHz"}, now=__import__("datetime").datetime(2026, 1, 1))
    cmds = "\n".join(out.commands)
    assert "uci set firewall.hn_portal80_dnat.dest_port=8099" in cmds and "uci set firewall.hn_portal80_in.src=hn80" in cmds
    plan["onboarding"]["offer"] = ["guest"]
    plan["onboarding"]["wifi"] = "home"
    text = "\n".join(n.validate(plan))
    assert "per-device keys" in text and "shared password" in text
    install = "\n".join(portal.install_lines(address="10.8.10.1", look=portal.look_from({}), offer=[("home", "home")]))
    assert "uci add_list uhttpd.hn_portal.listen_http=10.8.10.1:8099" in install and "uci set uhttpd.hn_portal.error_page=/cgi-bin/portal" in install


def test_wifi_qr_payload() -> None:
    assert qr.wifi_payload('My;Net"', "a:b,c\\d", "wpa2", hidden=True) == 'WIFI:T:WPA;S:My\\;Net\\";P:a\\:b\\,c\\\\d;H:true;;'
    assert qr.wifi_payload("Guest", None, "open") == "WIFI:T:nopass;S:Guest;;"
    assert qr.wifi_payload("Six", "k" * 8, "wpa3").startswith("WIFI:T:SAE;")
    assert qr.svg("WIFI:T:WPA;S:x;P:12345678;;").startswith("<svg")


@needs_sh
def test_files_are_written_one_line_at_a_time(tmp_path: Path) -> None:
    """An interactive SSH shell takes one line per command: no command may contain a newline."""
    look = portal.look_from({"title": "Hi 'there'", "text": "a\nb"})
    script = portal.cgi_script(look, [("home", "home")])
    page = portal.approved_page(look=look, name="TV", ssid="home", key="k'ey$1\\x", qr_svg=qr.svg("WIFI:T:WPA;S:home;P:12345678;;"), expires_text=None)
    lines = portal.install_lines(address="10.8.10.1", look=look, offer=[("home", "home")])
    lines += portal.approve_lines("021122334455", "abcdefghij012345", page)
    assert all("\n" not in cmd and "\r" not in cmd for cmd in lines)
    for chunk in n.chunks(lines):
        assert len(chunk) < 4000  # a terminal line has a 4096-byte limit
    writes = [c for c in lines if c.startswith((": >", "printf", "chmod", "mv", "mkdir"))]
    command = " && ".join(writes).replace(portal.WWW, str(tmp_path / "www")).replace(portal.TMP, str(tmp_path / "hn"))
    subprocess.run(["sh", "-c", f"mkdir -p {tmp_path / 'www' / 'cgi-bin'} && {command}"], check=True, timeout=20)
    expected = script.replace(portal.WWW, str(tmp_path / "www")).replace(portal.TMP, str(tmp_path / "hn"))  # paths moved like the commands
    assert (tmp_path / "www" / "cgi-bin" / "portal").read_text() == expected
    assert (tmp_path / "hn" / "ok" / "021122334455.html").read_text() == page
    assert (tmp_path / "hn" / "ok" / "021122334455.tok").read_text() == "abcdefghij012345"


LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
OWN_PAGE = (
    "<!doctype html>\n<html><head><title>{{title}}</title><style>{{style}} .brand{color:hotpink}</style></head>\n"
    "<body><header class=\"brand\">Acme Guest Access {{logo}}</header>\n<section>{{content}}</section>\n<footer>{{footer}}</footer></body></html>\n"
)


@needs_sh
def test_own_page_kinds_and_look(tmp_path: Path) -> None:
    settings = {
        "title": "Acme", "button": "Let me in", "waiting_text": "Hang on…", "footer": "Questions? Ask <Sam> & co.",
        "kinds": ["Laptop", "Console", "Laptop"], "ask_network": False, "logo": LOGO,
        "colors": {"accent": "#ff6600", "background": "#101820"}, "use_custom": True, "custom_html": OWN_PAGE,
    }
    assert portal.validate_look(settings) == []
    look = portal.look_from(settings)
    assert look.kinds == [("laptop", "Laptop"), ("console", "Console"), ("laptop_2", "Laptop")]
    arp = tmp_path / "arp"
    arp.write_text(f"IP address HW type Flags HW address Mask Device\n10.8.10.60 0x1 0x2 {MAC} * br\n")
    cgi = tmp_path / "p.sh"
    cgi.write_text(portal.cgi_script(look, [("home", "home")]).replace(portal.TMP, str(tmp_path / "hn")).replace(portal.ARP, str(arp)))

    def run(body: str | None = None) -> str:
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "REMOTE_ADDR": "10.8.10.60", "REQUEST_METHOD": "POST" if body else "GET",
               "CONTENT_LENGTH": str(len(body or "")), "HTTP_COOKIE": ""}
        return subprocess.run(["sh", str(cgi)], input=(body or "").encode(), capture_output=True, env=env, check=True, timeout=20).stdout.decode()

    page = run()
    assert '<header class="brand">Acme Guest Access <img class="logo" src="data:image/png;base64,' in page
    assert "--accent:#ff6600;--bg:#101820" in page and ".brand{color:hotpink}" in page
    assert "<section><h1>Acme</h1>" in page and ">Let me in</button>" in page and 'name="wants"' not in page
    assert '<option value="console">Console</option>' in page
    assert "<footer><div class=\"footer\">Questions? Ask &lt;Sam&gt; &amp; co.</div></footer>" in page
    out = run("name=Box&kind=console&wants=home")
    assert "Refresh: 5\r\n" in out and "Hang on…" in out
    stored = (tmp_path / "hn" / "req" / RID).read_text()
    assert "kind=console" in stored and "wants=\n" in stored  # devices don't choose the network


def test_look_validation_and_preview() -> None:
    bad = {"use_custom": True, "custom_html": "<p>no content marker</p>", "logo": "javascript:alert(1)", "colors": {"accent": "red"},
           "kinds": [str(i) for i in range(13)], "title": "x" * 61}
    text = "\n".join(portal.validate_look(bad))
    for expected in ("{{content}} exactly once", "PNG, JPEG", "colours", "12 device kinds", "title can have 60"):
        assert expected in text, expected
    look = portal.look_from(bad)  # unvalidated settings still give a safe page
    assert look.logo is None and look.colors["accent"] == portal.THEME_DEFAULTS["accent"]
    top, bottom = portal.frame(look)
    assert "<main>" in top and "</main>" in bottom  # falls back to the default page
    offer = [("home", "home")]
    good = portal.look_from({"title": "Hi", "approved_text": "{device} → {network}"})
    for page in ("form", "waiting", "declined", "approved"):
        html_page = portal.preview(good, offer, page, qr_svg="<svg></svg>")
        assert html_page.startswith("<!doctype html>") and "<h1>Hi</h1>" in html_page
    assert "Living room TV → <strong>home</strong>" in portal.preview(good, offer, "approved")
