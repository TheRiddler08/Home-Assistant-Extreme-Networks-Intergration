"""Register the Network sidebar panel and the dashboard cards.

The frontend is one JavaScript module without a build step
(``frontend/aerohive-network.js``). It defines the panel element and the
cards ``custom:aerohive-switch-card``, ``custom:aerohive-device-card`` and
``custom:aerohive-network-card``; it's loaded on every dashboard so the cards
work without adding a resource by hand.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}_frontend"
MODULE = "aerohive-network.js"
PANEL_URL_PATH = "aerohive-network"
PANEL_ELEMENT = "aerohive-network-panel"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the module, add the cards to dashboards and add the sidebar panel (once)."""
    if hass.data.get(_REGISTERED) or getattr(hass, "http", None) is None:
        return
    if "frontend" not in hass.config.components:
        return  # e.g. a minimal test instance
    hass.data[_REGISTERED] = True
    from homeassistant.components import frontend, panel_custom  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    version = (await async_get_integration(hass, DOMAIN)).version
    url = f"{URL_BASE}/{MODULE}?v={version}"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "frontend"), cache_headers=False)]
    )
    frontend.add_extra_js_url(hass, url)
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_ELEMENT,
            sidebar_title="Network",
            sidebar_icon="mdi:lan",
            module_url=url,
            require_admin=False,
            config={},
        )
    except ValueError:
        _LOGGER.debug("Network panel already registered")
