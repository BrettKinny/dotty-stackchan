"""Tests for dotty_doctor model-path resolution.

Regression: the model checks anchored `root = config_path.parent`, but the
standard deploy keeps config at `<root>/data/.config.yaml` while models are
bind-mounted from `<root>/models/`. So a healthy install FALSE-FAILed because
the checks looked under `<root>/data/models/`. `_project_root` now resolves to
whichever candidate actually contains `models/`.
"""
import importlib.util as _ilu
import pathlib
import tempfile
import unittest

_DOCTOR_PY = (
    pathlib.Path(__file__).parent.parent / "scripts" / "dotty_doctor.py"
)
_spec = _ilu.spec_from_file_location("dotty_doctor_under_test", _DOCTOR_PY)
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


class TestProjectRoot(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, *parts):
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return p

    # ── _project_root ────────────────────────────────────────────────────────

    def test_standard_layout_config_under_data(self):
        cfg = self._mk("data", ".config.yaml")
        (self.root / "models").mkdir()
        self.assertEqual(_mod._project_root(cfg), self.root)

    def test_legacy_layout_config_at_root(self):
        cfg = self._mk(".config.yaml")
        (self.root / "models").mkdir()
        self.assertEqual(_mod._project_root(cfg), self.root)

    def test_none_config_uses_cwd(self):
        self.assertEqual(_mod._project_root(None), pathlib.Path.cwd())

    def test_missing_models_returns_repo_root_not_data(self):
        # No models/ anywhere → still resolves to repo root (parent of data/),
        # so the FAIL message points at <root>/models, not <root>/data/models.
        cfg = self._mk("data", ".config.yaml")
        self.assertEqual(_mod._project_root(cfg), self.root)

    # ── end-to-end checks in the standard (data/) layout ─────────────────────

    def test_piper_check_passes_with_models_at_repo_root(self):
        cfg = self._mk("data", ".config.yaml")
        self._mk("models", "piper", "voice.onnx")
        res = _mod.check_models_piper(cfg)
        self.assertEqual(res.status, "pass", res.detail)

    def test_sensevoice_check_passes_with_models_at_repo_root(self):
        cfg = self._mk("data", ".config.yaml")
        orig = _mod.SENSEVOICE_REQUIRED
        # Shrink the size floors so the test doesn't need a 200 MB model.pt.
        _mod.SENSEVOICE_REQUIRED = {name: 1 for name in orig}
        try:
            for name in _mod.SENSEVOICE_REQUIRED:
                self._mk("models", "SenseVoiceSmall", name)
            res = _mod.check_models_sensevoice(cfg)
            self.assertEqual(res.status, "pass", res.detail)
        finally:
            _mod.SENSEVOICE_REQUIRED = orig

    def test_sensevoice_check_fails_pointing_at_repo_root_models(self):
        cfg = self._mk("data", ".config.yaml")
        res = _mod.check_models_sensevoice(cfg)
        self.assertEqual(res.status, "fail")
        # Must blame <root>/models/..., never <root>/data/models/...
        self.assertIn(str(self.root / "models" / "SenseVoiceSmall"), res.detail)
        self.assertNotIn("data/models", res.detail)


if __name__ == "__main__":
    unittest.main()
