"""Test configuration.

The parser/command/client tests only need the HA-free modules. When Home
Assistant (and pytest-homeassistant-custom-component) is installed, the real
package is used and the HA integration tests in test_init.py run too; when it
is not, lightweight package stubs make the pure modules importable and the HA
tests are skipped.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "aerohive_local"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:
    import homeassistant  # noqa: F401
    import pytest_homeassistant_custom_component  # noqa: F401

    HAS_HA = True
except ImportError:
    HAS_HA = False

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if not HAS_HA and "custom_components.aerohive_local" not in sys.modules:
    cc = types.ModuleType("custom_components")
    cc.__path__ = [str(ROOT / "custom_components")]
    pkg = types.ModuleType("custom_components.aerohive_local")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules.setdefault("custom_components", cc)
    sys.modules["custom_components.aerohive_local"] = pkg


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


if HAS_HA:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ANN001
        """Allow loading custom_components in every HA test."""
        return
