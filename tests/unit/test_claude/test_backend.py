"""Tests for the backend abstraction + router."""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.backend import (
    BACKEND_DISPLAY,
    BackendRouter,
    SdkBackend,
)
from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeResponse
from src.config.settings import Settings


def _settings(**overrides: Any) -> Settings:
    base: Dict[str, Any] = {
        "telegram_bot_token": "test:token",
        "telegram_bot_username": "testbot",
        "approved_directory": Path("."),
    }
    base.update(overrides)
    return Settings(**base)


def _fake_backend(name: str) -> MagicMock:
    backend = MagicMock()
    backend.name = name
    backend.describe.return_value = f"{name} backend"
    backend.execute_command = AsyncMock(
        return_value=ClaudeResponse(
            content="ok",
            session_id=f"session-{name}",
            cost=0.0,
            duration_ms=1,
            num_turns=1,
        )
    )
    backend.status = AsyncMock(return_value={"ok": True, "backend": name})
    return backend


class TestBackendRouter:
    def test_select_default_from_config(self) -> None:
        config = _settings(claude_backend="desktop")
        backends = {
            "sdk": _fake_backend("sdk"),
            "desktop": _fake_backend("desktop"),
            "cowork": _fake_backend("cowork"),
        }
        router = BackendRouter(config, backends)
        assert router.select().name == "desktop"

    def test_select_chat_override_wins(self) -> None:
        config = _settings(claude_backend="sdk")
        backends = {
            "sdk": _fake_backend("sdk"),
            "desktop": _fake_backend("desktop"),
        }
        router = BackendRouter(config, backends)
        assert router.select("desktop").name == "desktop"

    def test_select_invalid_override_falls_back(self) -> None:
        config = _settings(claude_backend="sdk")
        backends = {
            "sdk": _fake_backend("sdk"),
            "cowork": _fake_backend("cowork"),
        }
        router = BackendRouter(config, backends)
        assert router.select("nonsense").name == "sdk"

    def test_select_missing_configured_falls_back_to_sdk(self) -> None:
        config = _settings(claude_backend="desktop")
        backends = {"sdk": _fake_backend("sdk")}
        router = BackendRouter(config, backends)
        assert router.select().name == "sdk"

    def test_no_backends_raises(self) -> None:
        config = _settings(claude_backend="sdk")
        router = BackendRouter(config, {})
        with pytest.raises(RuntimeError):
            router.select()

    def test_available_backends_canonical_order(self) -> None:
        config = _settings()
        backends = {
            "cowork": _fake_backend("cowork"),
            "sdk": _fake_backend("sdk"),
            "desktop": _fake_backend("desktop"),
        }
        router = BackendRouter(config, backends)
        assert router.available_backends() == ["sdk", "desktop", "cowork"]

    async def test_status_reports_all(self) -> None:
        config = _settings()
        backends = {
            "sdk": _fake_backend("sdk"),
            "desktop": _fake_backend("desktop"),
        }
        router = BackendRouter(config, backends)
        status = await router.status()
        assert set(status) == {"sdk", "desktop"}
        assert status["sdk"]["ok"] is True


class TestSdkBackend:
    async def test_execute_passes_through(self) -> None:
        config = _settings()
        sdk_manager = MagicMock()
        sdk_manager.execute_command = AsyncMock(
            return_value=ClaudeResponse(
                content="hi",
                session_id="s1",
                cost=0.0,
                duration_ms=1,
                num_turns=1,
            )
        )
        backend = SdkBackend(config, sdk_manager)
        result = await backend.execute_command(
            prompt="hello", working_directory=Path(".")
        )
        assert result.content == "hi"
        sdk_manager.execute_command.assert_awaited_once()
        kwargs = sdk_manager.execute_command.await_args.kwargs
        assert kwargs["prompt"] == "hello"

    async def test_status_shape(self) -> None:
        config = _settings(claude_model="claude-sonnet-4-5")
        backend = SdkBackend(config)
        status = await backend.status()
        assert status["ok"] is True
        assert status["backend"] == "sdk"
        assert status["model"] == "claude-sonnet-4-5"


class TestFacadeRouting:
    async def test_facade_routes_to_selected_backend(self) -> None:
        config = _settings(claude_backend="cowork")
        session_manager = MagicMock()
        session_manager.get_or_create_session = AsyncMock(
            return_value=MagicMock(is_new_session=True, session_id="temp")
        )
        session_manager.update_session = AsyncMock()
        session_manager.remove_session = AsyncMock()
        session_manager._get_user_sessions = AsyncMock(return_value=[])

        sdk = _fake_backend("sdk")
        desktop = _fake_backend("desktop")
        cowork = _fake_backend("cowork")
        integration = ClaudeIntegration(
            config=config,
            session_manager=session_manager,
            backends={"sdk": sdk, "desktop": desktop, "cowork": cowork},
        )
        result = await integration.run_command(
            prompt="test", working_directory=Path("."), user_id=1
        )
        assert result.content == "ok"
        cowork.execute_command.assert_awaited_once()
        sdk.execute_command.assert_not_awaited()

    async def test_facade_per_chat_backend_override(self) -> None:
        config = _settings(claude_backend="sdk")
        session_manager = MagicMock()
        session_manager.get_or_create_session = AsyncMock(
            return_value=MagicMock(is_new_session=True, session_id="temp")
        )
        session_manager.update_session = AsyncMock()
        session_manager._get_user_sessions = AsyncMock(return_value=[])

        desktop = _fake_backend("desktop")
        sdk = _fake_backend("sdk")
        integration = ClaudeIntegration(
            config=config,
            session_manager=session_manager,
            backends={"sdk": sdk, "desktop": desktop},
        )
        await integration.run_command(
            prompt="test",
            working_directory=Path("."),
            user_id=1,
            backend="desktop",
        )
        desktop.execute_command.assert_awaited_once()

    async def test_get_backend_status(self) -> None:
        config = _settings()
        integration = ClaudeIntegration(
            config=config,
            backends={
                "sdk": _fake_backend("sdk"),
                "desktop": _fake_backend("desktop"),
            },
        )
        status = await integration.get_backend_status()
        assert status["active_default"] == "sdk"
        assert "sdk" in status["available"]

    def test_backend_display(self) -> None:
        assert BACKEND_DISPLAY["desktop"] == "Claude Desktop mirror"
