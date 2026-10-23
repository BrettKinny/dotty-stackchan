"""The TTS provider module is imported once at server start.

2026-10-23 A/V run: after a restart the robot connected twice in one second.
Both connections built their TTS on worker threads, and upstream's
create_instance found the module half-imported in sys.modules ("partially
initialized module 'core.providers.tts.piper_local' has no attribute
'TTSProvider'"). The surviving connection had no TTS, so Dotty couldn't speak
until it reconnected.

AI-assisted (Claude Opus 5.5).
"""
import importlib.util as _ilu
import pathlib
import sys
import types
import unittest

_PATCHES = pathlib.Path(__file__).parent.parent / "custom-providers" / "xiaozhi-patches"


def _ns(**attrs):
    return types.SimpleNamespace(**attrs)


_STUBS = {
    "websockets": _ns(ServerConnection=object, Request=object, Response=object),
    "config": _ns(), "config.logger": _ns(setup_logging=lambda: None),
    "config.config_loader": _ns(get_config_from_api_async=None),
    "core": _ns(), "core.connection": _ns(ConnectionHandler=object),
    "core.auth": _ns(AuthManager=object, AuthenticationError=Exception),
    "core.utils": _ns(), "core.utils.modules_initialize": _ns(initialize_modules=None),
    "core.utils.util": _ns(check_vad_update=None, check_asr_update=None),
    "core.portal_bridge": _ns(active_connections={}, restore_device_state=None),
}


def _load():
    saved = {name: sys.modules.get(name) for name in _STUBS}
    sys.modules.update(_STUBS)
    try:
        spec = _ilu.spec_from_file_location("websocket_server_under_test",
                                            _PATCHES / "websocket_server.py")
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


ws = _load()


class PreimportTts(unittest.TestCase):
    def _run(self, config, present=True):
        imported = []
        ws._dotty_preimport_tts(config, import_module=imported.append,
                                exists=lambda path: present)
        return imported

    def test_selected_provider_is_imported(self):
        config = {"selected_module": {"TTS": "LocalPiper"},
                  "TTS": {"LocalPiper": {"type": "piper_local"}}}
        self.assertEqual(self._run(config), ["core.providers.tts.piper_local"])

    def test_name_is_the_type_when_no_type_is_given(self):
        config = {"selected_module": {"TTS": "edge"}, "TTS": {"edge": {}}}
        self.assertEqual(self._run(config), ["core.providers.tts.edge"])

    def test_missing_or_broken_config_never_raises(self):
        self.assertEqual(self._run({"selected_module": {}}), [])
        self.assertEqual(self._run({"selected_module": {"TTS": "x"}, "TTS": {"x": {}}},
                                   present=False), [])

        def boom(name):
            raise ImportError(name)
        ws._dotty_preimport_tts({"selected_module": {"TTS": "x"}, "TTS": {"x": {}}},
                                import_module=boom, exists=lambda path: True)
