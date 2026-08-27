"""Production-shaped regression tests for reconnect state ownership (#170)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock


_ROOT = Path(__file__).resolve().parents[1]
_PATCHES = _ROOT / "custom-providers" / "xiaozhi-patches"


class _Logger:
    def bind(self, **_kwargs):
        return self

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def _load_portal() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "state_reconnect_portal_under_test", _PATCHES / "portal_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _stub_registry_imports(portal):
    """Load the real event handler while replacing xiaozhi-only imports."""
    names = {
        "core",
        "core.handle",
        "core.handle.textHandler",
        "core.handle.textMessageHandler",
        "core.handle.textHandler.abortMessageHandler",
        "core.handle.textHandler.helloMessageHandler",
        "core.handle.textHandler.iotMessageHandler",
        "core.handle.textHandler.listenMessageHandler",
        "core.handle.textHandler.mcpMessageHandler",
        "core.handle.textHandler.pingMessageHandler",
        "core.handle.textHandler.serverMessageHandler",
        "core.portal_bridge",
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in names}

    class Base:
        pass

    try:
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["core.portal_bridge"] = portal
        sys.modules["core.handle.textMessageHandler"].TextMessageHandler = Base
        classes = {
            "abortMessageHandler": "AbortTextMessageHandler",
            "helloMessageHandler": "HelloTextMessageHandler",
            "iotMessageHandler": "IotTextMessageHandler",
            "listenMessageHandler": "ListenTextMessageHandler",
            "mcpMessageHandler": "McpTextMessageHandler",
            "pingMessageHandler": "PingMessageHandler",
            "serverMessageHandler": "ServerTextMessageHandler",
        }
        for module_name, cls_name in classes.items():
            module = sys.modules[f"core.handle.textHandler.{module_name}"]
            setattr(module, cls_name, type(cls_name, (), {}))
        spec = importlib.util.spec_from_file_location(
            "state_reconnect_registry_under_test",
            _PATCHES / "textMessageHandlerRegistry.py",
        )
        assert spec is not None and spec.loader is not None
        registry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(registry)
        yield registry
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@contextmanager
def _set_env(**values):
    saved = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_event_handler_records_firmware_state_for_device_reconnect() -> None:
    portal = _load_portal()
    portal.last_known_states.clear()
    with _stub_registry_imports(portal) as registry:
        registry._spawn = lambda coro, **_kwargs: coro.close()
        conn = types.SimpleNamespace(
            headers={"device-id": "dev-1"}, logger=_Logger()
        )
        with _set_env(BRIDGE_URL="http://behaviour"):
            asyncio.run(registry.EventTextMessageHandler().handle(
                conn,
                {"name": "state_changed", "data": {"state": "SECURITY"}},
            ))

    assert conn.current_state == "security"
    assert conn._dotty_desired_state == "security"
    assert portal.state_for_device("dev-1") == "security"


def test_websocket_server_seeds_replacement_handler_from_last_state() -> None:
    portal = _load_portal()
    portal.active_connections.clear()
    portal.last_known_states.clear()
    portal.last_known_states["dev-1"] = "sleep"

    names = {
        "websockets",
        "config",
        "config.logger",
        "config.config_loader",
        "core",
        "core.connection",
        "core.auth",
        "core.utils",
        "core.utils.modules_initialize",
        "core.utils.util",
        "core.portal_bridge",
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in names}
    try:
        websockets = types.ModuleType("websockets")
        websockets.ServerConnection = object
        websockets.serve = MagicMock()
        sys.modules["websockets"] = websockets

        logger_mod = types.ModuleType("config.logger")
        logger_mod.setup_logging = lambda: _Logger()
        sys.modules["config.logger"] = logger_mod
        sys.modules["config"] = types.ModuleType("config")
        loader = types.ModuleType("config.config_loader")
        loader.get_config_from_api_async = None
        sys.modules["config.config_loader"] = loader

        class FakeConnection:
            instances = []

            def __init__(self, *_args):
                self.current_state = "idle"
                self.headers = {}
                self.is_handled = False
                type(self).instances.append(self)

            async def handle_connection(self, _websocket):
                self.is_handled = True

        connection_mod = types.ModuleType("core.connection")
        connection_mod.ConnectionHandler = FakeConnection
        sys.modules["core.connection"] = connection_mod
        auth_mod = types.ModuleType("core.auth")
        auth_mod.AuthManager = lambda **_kwargs: object()
        auth_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        sys.modules["core.auth"] = auth_mod
        init_mod = types.ModuleType("core.utils.modules_initialize")
        init_mod.initialize_modules = lambda *_args: {}
        sys.modules["core.utils.modules_initialize"] = init_mod
        util_mod = types.ModuleType("core.utils.util")
        util_mod.check_vad_update = lambda *_args: False
        util_mod.check_asr_update = lambda *_args: False
        sys.modules["core.utils.util"] = util_mod
        sys.modules["core"] = types.ModuleType("core")
        sys.modules["core.utils"] = types.ModuleType("core.utils")
        sys.modules["core.portal_bridge"] = portal

        spec = importlib.util.spec_from_file_location(
            "state_reconnect_websocket_under_test", _PATCHES / "websocket_server.py"
        )
        assert spec is not None and spec.loader is not None
        websocket_server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(websocket_server)

        server = websocket_server.WebSocketServer({
            "selected_module": {},
            "server": {"auth_key": "test", "auth": {"enabled": False}},
        })

        class FakeWebSocket:
            closed = False

            def __init__(self):
                self.request = types.SimpleNamespace(
                    headers={"device-id": "dev-1"}, path="/"
                )

            async def close(self):
                self.closed = True

        asyncio.run(server._handle_connection(FakeWebSocket()))
        handler = FakeConnection.instances[-1]
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    assert handler.is_handled
    assert handler.current_state == "sleep"
    assert handler._dotty_desired_state == "sleep"
