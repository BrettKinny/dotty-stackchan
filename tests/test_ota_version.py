"""Unit tests for the OTA version comparator (ota_handler.py).

Covers the pre-release / build-suffix precedence bug: the old
``re.findall(r"\\d+")`` parser turned ``1.2.3-rc1`` into ``(1,2,3,1)`` and
ranked it ABOVE GA ``1.2.3``, so a device on GA would be offered a stale rc.
The module's core.* / aiohttp imports are stubbed so the test runs without a
container; only the pure module-level functions are exercised.
"""
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock

# ── Stub the container-only imports ota_handler pulls at load time ────────────
# NB: do NOT stub `aiohttp` — it's a real installed dep that other test modules
# in this suite import for real; stubbing it here (setdefault wins on import
# order) would poison sys.modules and break them.
for _n in ("core", "core.auth", "core.utils", "core.utils.util", "core.api"):
    sys.modules.setdefault(_n, MagicMock())

# BaseHandler is used as a real base class (`class OTAHandler(BaseHandler)`), so
# it must be a genuine type, not a MagicMock attribute.
_base_mod = types.ModuleType("core.api.base_handler")


class _StubBase:
    def __init__(self, *a, **k):
        pass


_base_mod.BaseHandler = _StubBase  # type: ignore[attr-defined]
sys.modules["core.api.base_handler"] = _base_mod

# ── Load the module under test by path ───────────────────────────────────────
import importlib.util as _ilu

_OTA_PY = (
    pathlib.Path(__file__).parent.parent
    / "custom-providers"
    / "xiaozhi-patches"
    / "ota_handler.py"
)
_spec = _ilu.spec_from_file_location("ota_handler_under_test", _OTA_PY)
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_is_higher = _mod._is_higher_version
_key = _mod._parse_version


class TestOtaVersion(unittest.TestCase):

    # The headline regression: a pre-release must NOT outrank its GA release.
    def test_prerelease_not_higher_than_release(self):
        self.assertFalse(_is_higher("1.2.3-rc1", "1.2.3"))
        self.assertTrue(_is_higher("1.2.3", "1.2.3-rc1"))

    def test_build_suffix_not_higher_than_release(self):
        self.assertFalse(_is_higher("1.2.3+build5", "1.2.3"))
        self.assertTrue(_is_higher("1.2.3", "1.2.3+build5"))

    def test_normal_ordering(self):
        self.assertTrue(_is_higher("1.2.4", "1.2.3"))
        self.assertTrue(_is_higher("2.0.0", "1.9.9"))
        self.assertTrue(_is_higher("1.3.0", "1.2.9"))
        self.assertFalse(_is_higher("1.2.3", "1.2.4"))

    def test_equal_versions_are_not_higher(self):
        self.assertFalse(_is_higher("1.2.3", "1.2.3"))
        # short form equals its zero-padded form (1.2 == 1.2.0)
        self.assertFalse(_is_higher("1.2", "1.2.0"))
        self.assertFalse(_is_higher("1.2.0", "1.2"))
        self.assertEqual(_key("1.2"), _key("1.2.0"))

    def test_leading_v_stripped(self):
        self.assertFalse(_is_higher("v1.2.3", "1.2.3"))
        self.assertTrue(_is_higher("v1.2.4", "v1.2.3"))

    def test_prerelease_ordering_is_deterministic(self):
        # Among pre-releases of the same core, lexical suffix order applies.
        self.assertTrue(_is_higher("1.0.0-beta", "1.0.0-alpha"))
        self.assertFalse(_is_higher("1.0.0-alpha", "1.0.0-beta"))

    def test_only_first_three_segments_considered(self):
        # A 4th numeric segment must not flip precedence vs the 3-segment GA.
        self.assertFalse(_is_higher("1.2.3.4", "1.2.3"))
        self.assertEqual(_key("1.2.3.4")[:3], (1, 2, 3))

    def test_key_is_sortable(self):
        versions = ["1.2.3", "1.2.3-rc1", "1.10.0", "1.2.10", "2.0.0", "v1.2.4"]
        ordered = sorted(versions, key=_key)
        # GA 1.2.3 sorts above its rc; numeric (not lexical) segment compare.
        self.assertLess(ordered.index("1.2.3-rc1"), ordered.index("1.2.3"))
        self.assertLess(ordered.index("1.2.3"), ordered.index("1.2.10"))
        self.assertLess(ordered.index("1.2.10"), ordered.index("1.10.0"))
        self.assertEqual(ordered[-1], "2.0.0")


if __name__ == "__main__":
    unittest.main()
