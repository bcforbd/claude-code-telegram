"""Tests for the Claude Cowork backend (profile import)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.claude.cowork_integration import (
    COWORK_MODEL_DEFAULT,
    CoworkManager,
    _extract_preferences_text,
)
from src.config.settings import Settings


def _settings(**overrides) -> Settings:
    """Settings with required fields filled in."""
    base = {
        "telegram_bot_token": "test:token",
        "telegram_bot_username": "testbot",
        "approved_directory": Path("."),
    }
    base.update(overrides)
    return Settings(**base)


class TestPreferencesExtraction:
    def test_string_preferences(self) -> None:
        assert _extract_preferences_text("Be concise.") == "Be concise."

    def test_list_of_strings(self) -> None:
        value = ["Be concise.", "Use bullets."]
        assert _extract_preferences_text(value) == "Be concise.\nUse bullets."

    def test_list_of_blocks(self) -> None:
        value = [
            {"type": "text", "content": "Part one."},
            {"type": "text", "content": "Part two."},
        ]
        assert _extract_preferences_text(value) == "Part one.\nPart two."

    def test_mixed_and_empty(self) -> None:
        value = [{"type": "text", "content": ""}, "  ", {"type": "x", "content": "y"}]
        assert _extract_preferences_text(value) == "y"

    def test_none_and_empty(self) -> None:
        assert _extract_preferences_text(None) is None
        assert _extract_preferences_text("   ") is None
        assert _extract_preferences_text([]) is None


class TestCoworkSettings:
    def _write_settings(self, path: Path, data: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_explicit_path(self, tmp_path) -> None:
        path = self._write_settings(
            tmp_path / "cowork_account_settings.json",
            {"conversation_preferences": "Be direct."},
        )
        manager = CoworkManager(_settings(cowork_settings_path=path))
        assert manager.cowork_settings_path() == path
        assert manager.conversation_preferences() == "Be direct."

    def test_missing_path_returns_none(self) -> None:
        manager = CoworkManager(
            _settings(cowork_settings_path=Path("Z:/nope/none.json"))
        )
        assert manager.cowork_settings_path() is None
        assert manager.conversation_preferences() is None

    def test_discovery_prefers_meaningful_profile(self, tmp_path, monkeypatch) -> None:
        sessions = tmp_path / "Claude-3p" / "local-agent-mode-sessions"
        empty = sessions / "user1" / "00000000"
        real = sessions / "user1" / "user1"
        empty.mkdir(parents=True)
        real.mkdir(parents=True)
        (empty / "cowork_account_settings.json").write_text(
            json.dumps({}), encoding="utf-8"
        )
        (real / "cowork_account_settings.json").write_text(
            json.dumps({"__model_selector_state": {"model": "auto/claude-opus"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            CoworkManager,
            "_default_search_roots",
            lambda self: [tmp_path / "Claude-3p"],
        )
        manager = CoworkManager(_settings())
        assert manager.cowork_settings_path() == real / "cowork_account_settings.json"

    def test_model_resolution_priority(self, tmp_path) -> None:
        # 1. Explicit env config wins
        manager = CoworkManager(_settings(cowork_model="claude-sonnet-4-5"))
        assert manager.resolved_model() == "claude-sonnet-4-5"

        # 2. Account model selector (3p layout)
        path = self._write_settings(
            tmp_path / "cowork_account_settings.json",
            {"__model_selector_state": {"cowork": {"model": "auto/claude-sonnet-4-5"}}},
        )
        manager = CoworkManager(_settings(cowork_settings_path=path))
        assert manager.resolved_model() == "auto/claude-sonnet-4-5"

        # 3. Cowork default
        manager = CoworkManager(_settings())
        assert manager.resolved_model() == COWORK_MODEL_DEFAULT

    def test_plugin_dirs_only_existing(self, tmp_path) -> None:
        existing = tmp_path / "plugin-a"
        existing.mkdir()
        config = _settings(cowork_plugin_dirs=[existing, tmp_path / "missing"])
        manager = CoworkManager(config)
        plugins = manager.plugin_dirs()
        assert len(plugins) == 1
        assert plugins[0]["path"] == str(existing)

    def test_preferences_inside_account_profile(self, tmp_path) -> None:
        path = self._write_settings(
            tmp_path / "cowork_account_settings.json",
            {
                "__account_profile": {
                    "conversation_preferences": "Follow the Cavepathy protocol."
                }
            },
        )
        manager = CoworkManager(_settings(cowork_settings_path=path))
        assert manager.conversation_preferences() == "Follow the Cavepathy protocol."

    def test_preferences_top_level_wins(self, tmp_path) -> None:
        path = self._write_settings(
            tmp_path / "cowork_account_settings.json",
            {
                "conversation_preferences": "Top level.",
                "__account_profile": {"conversation_preferences": "Nested."},
            },
        )
        manager = CoworkManager(_settings(cowork_settings_path=path))
        assert manager.conversation_preferences() == "Top level."

    async def test_status_shape(self, tmp_path) -> None:
        path = self._write_settings(
            tmp_path / "cowork_account_settings.json",
            {"conversation_preferences": "Be direct."},
        )
        manager = CoworkManager(_settings(cowork_settings_path=path))
        status = await manager.status()
        assert status["ok"] is True
        assert status["backend"] == "cowork"
        assert status["model"] == COWORK_MODEL_DEFAULT
        assert status["permission_mode"] == "acceptEdits"
        assert status["web_search"] is True
        assert status["preferences_loaded"] is True


class TestCoworkExecution:
    async def test_execute_applies_profile(self, tmp_path) -> None:
        settings_path = tmp_path / "cowork_account_settings.json"
        settings_path.write_text(
            json.dumps({"conversation_preferences": "Be direct."}),
            encoding="utf-8",
        )
        sdk_manager = MagicMock()
        sdk_manager.execute_command = AsyncMock()
        manager = CoworkManager(
            _settings(
                cowork_settings_path=settings_path,
                cowork_model="claude-sonnet-4-5",
            ),
            sdk_manager=sdk_manager,
        )
        await manager.execute_command(prompt="Summarize", working_directory=tmp_path)
        kwargs = sdk_manager.execute_command.await_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-5"
        assert kwargs["permission_mode"] == "acceptEdits"
        assert kwargs["allowed_tools_extra"] == ["WebSearch"]
        assert "Be direct." in kwargs["system_prompt_extra"]

    async def test_execute_web_search_disabled(self, tmp_path) -> None:
        sdk_manager = MagicMock()
        sdk_manager.execute_command = AsyncMock()
        manager = CoworkManager(
            _settings(cowork_enable_web_search=False),
            sdk_manager=sdk_manager,
        )
        await manager.execute_command(prompt="x", working_directory=tmp_path)
        kwargs = sdk_manager.execute_command.await_args.kwargs
        assert kwargs["allowed_tools_extra"] is None
        assert kwargs["model"] == COWORK_MODEL_DEFAULT
