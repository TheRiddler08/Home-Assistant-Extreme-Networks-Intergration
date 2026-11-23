"""The onboarding portal served by Home Assistant (for routers that can't serve it, e.g. OPNsense).

Same pages and rules as the router-hosted portal (portal.py): a device on the
onboarding network opens http://<router address>:8099/, the router forwards that
to Home Assistant on port 8099, the device asks for access, you approve it in
Home Assistant and the device's page shows its key.

A device is recognised by its MAC address, looked up from its IP in the router's
ARP table (the router doesn't hide it: the request is forwarded, not proxied),
and by a cookie set when it asked, so only the browser that asked sees the key.
Only GET and POST of the portal page are answered; nothing else of Home Assistant
is reachable through this port.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import portal

if TYPE_CHECKING:
    from .plan import PlanManager

_LOGGER = logging.getLogger(__name__)

COOKIE = "hn_token"
MAX_FORM = 4096
_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]")


def _token() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


class PortalServer:
    def __init__(self, manager: PlanManager, port: int | None = None) -> None:
        self.manager = manager
        self.port = port or portal.PORT
        self._runner: web.AppRunner | None = None
        self._hits: deque[float] = deque()

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def async_start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(client_max_size=MAX_FORM)
        app.router.add_get("/", self._get)
        app.router.add_get("/cgi-bin/portal", self._get)
        app.router.add_post("/cgi-bin/portal", self._post)
        app.router.add_post("/", self._post)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=None, port=self.port)
        try:
            await site.start()
        except OSError as err:
            await runner.cleanup()
            raise OSError(
                f"port {self.port} is in use on the Home Assistant host ({err})"
            ) from err
        self._runner = runner
        _LOGGER.info("Onboarding portal listening on port %s", self.port)

    async def async_stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ pages

    def _page(
        self,
        content: str,
        *,
        status: int = 200,
        cookie: str | None = None,
        refresh: bool = False,
    ) -> web.Response:
        look = portal.look_from(self.manager.plan["onboarding"])
        top, bottom = portal.frame(look)
        response = web.Response(
            text=top + content + bottom,
            status=status,
            content_type="text/html",
            charset="utf-8",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if refresh:
            response.headers["Refresh"] = "5"
        if cookie:
            response.set_cookie(
                COOKIE, cookie, max_age=86400, httponly=True, samesite="Lax", path="/"
            )
        return response

    def _look(self) -> portal.Look:
        return portal.look_from(self.manager.plan["onboarding"])

    def _who(self, request: web.Request) -> tuple[str, str] | None:
        """(request id, MAC) of the device asking, from its IP in the router's ARP table."""
        ip = request.remote or ""
        mac = self.manager.portal_mac(ip)
        return (mac.replace(":", ""), mac) if mac else None

    def _status_page(self, request: web.Request, rid: str) -> web.Response | None:
        look = self._look()
        item = self.manager.requests.get(rid)
        if item is None:
            return None
        if item["status"] == "pending":
            return self._page(portal.waiting_html(look), refresh=True)
        if item["status"] == "declined":
            return self._page(portal.declined_html(look))
        if item["status"] == "approved":
            if request.cookies.get(COOKIE) != item.get("token"):
                return self._page(portal.other_browser_html(look))
            page = self.manager.portal_pages.get(rid)
            return (
                self._page(page)
                if page
                else self._page(portal.other_browser_html(look))
            )
        return None

    async def _get(self, request: web.Request) -> web.Response:
        if not self.manager.portal_hosted():
            raise web.HTTPNotFound
        who = self._who(request)
        if who is None:
            return self._page(portal.unknown_html(self._look()))
        shown = self._status_page(request, who[0])
        if shown is not None:
            return shown
        return self._page(portal.form_html(self._look(), self.manager.portal_offer()))

    async def _post(self, request: web.Request) -> web.Response:
        if not self.manager.portal_hosted():
            raise web.HTTPNotFound
        look = self._look()
        who = self._who(request)
        if who is None:
            return self._page(portal.unknown_html(look))
        rid, mac = who
        shown = self._status_page(request, rid)
        if shown is not None:
            return shown
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 3600:
            self._hits.popleft()
        limit = int(self.manager.plan["onboarding"].get("max_per_hour") or 10)
        pending = sum(
            1 for r in self.manager.requests.values() if r["status"] == "pending"
        )
        if len(self._hits) >= limit or pending >= portal.MAX_PENDING:
            return self._page(portal.busy_html(look), status=429)
        self._hits.append(now)
        form = await request.post()
        name = (
            _SAFE_RE.sub("", str(form.get("name") or "")).strip()[:40]
            or "Unnamed device"
        )
        kinds = [k for k, _ in look.kinds]
        kind = str(form.get("kind") or "")
        kind = kind if kind in kinds else kinds[-1]
        offer = [i for i, _ in self.manager.portal_offer()] if look.ask_network else []
        wants = str(form.get("wants") or "")
        token = _token()
        item = {
            "id": rid, "mac": mac, "ip": request.remote, "name": name, "kind": kind,
            "wants": wants if wants in offer else None, "time": int(time.time()), "token": token,
        }  # fmt: skip
        await self.manager.async_portal_request(item)
        status = self.manager.requests.get(rid, {}).get("status")
        if status == "approved":  # approved straight away by a rule
            return self._page(
                self.manager.portal_pages.get(rid) or portal.other_browser_html(look),
                cookie=token,
            )
        return self._page(portal.waiting_html(look), cookie=token, refresh=True)

    def describe(self) -> dict[str, Any]:
        return {
            "hosted_by": "home_assistant",
            "port": self.port,
            "running": self.running,
        }
