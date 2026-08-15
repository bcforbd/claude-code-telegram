"""Tests for the Claude Desktop backend (config import)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.claude.desktop_integration import ClaudeDesktopManager
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


def _write_desktop_config(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestDesktopConfigParsing:
    def test_config_path_resolution_default(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path))
        manager = ClaudeDesktopManager(_settings())
        assert (
            manager.desktop_config_path()
            == tmp_path / "Claude" / "claude_desktop_config.json"
        )

    def test_config_path_override(self, tmp_path) -> None:
        custom = tmp_path / "custom" / "config.json"
        manager = ClaudeDesktopManager(_settings(desktop_config_path=custom))
        assert manager.desktop_config_path() == custom

    def test_load_missing_config_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
        manager = ClaudeDesktopManager(_settings())
        assert manager.load_desktop_config() is None

    async def test_missing_config_status_not_ok(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
        manager = ClaudeDesktopManager(_settings())
        status = await manager.status()
        assert status["ok"] is False

    def test_load_invalid_json_returns_none(self, tmp_path, monkeypatch) -> None:
        base = tmp_path / "appdata"
        path = base / "Claude" / "claude_desktop_config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(base))
        manager = ClaudeDesktopManager(_settings())
        assert manager.load_desktop_config() is None

    def test_mcp_servers_extracted(self, tmp_path, monkeypatch) -> None:
        base = tmp_path / "appdata"
        _write_desktop_config(
            base / "Claude" / "claude_desktop_config.json",
            {
                "mcpServers": {
                    "desktop-commander": {
                        "command": "npx",
                        "args": ["-y", "@wonderwhy-er/desktop-commander"],
                    },
                    "telegram": {
                        "command": "bun",
                        "args": ["server.ts"],
                    },
                    "broken": {"no_command": True},
                },
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example.com",
                    "SOME_FLAG": True,
                },
            },
        )
        monkeypatch.setenv("APPDATA", str(base))
        manager = ClaudeDesktopManager(_settings())
        servers = manager.mcp_servers()
        assert set(servers) == {"desktop-commander", "telegram"}
        assert servers["telegram"]["command"] == "bun"

    def test_env_overrides_extracted(self, tmp_path, monkeypatch) -> None:
        base = tmp_path / "appdata"
        _write_desktop_config(
            base / "Claude" / "claude_desktop_config.json",
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example.com",
                    "ANTHROPIC_API_KEY": "sk-test",
                    "EMPTY": "",
                }
            },
        )
        monkeypatch.setenv("APPDATA", str(base))
        manager = ClaudeDesktopManager(_settings())
        env = manager.env_overrides()
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
        assert env["ANTHROPIC_API_KEY"] == "sk-test"
        assert env["EMPTY"] == ""

    async def test_status_shape(self, tmp_path, monkeypatch) -> None:
        base = tmp_path / "appdata"
        _write_desktop_config(
            base / "Claude" / "claude_desktop_config.json",
            {
                "mcpServers": {"desktop-commander": {"command": "npx"}},
                "env": {"ANTHROPIC_BASE_URL": "https://gw.local"},
            },
        )
        monkeypatch.setenv("APPDATA", str(base))
        manager = ClaudeDesktopManager(_settings())
        status = await manager.status()
        assert status["ok"] is True
        assert status["mcp_servers"] == ["desktop-commander"]
        assert status["gateway_base_url"] == "https://gw.local"
        assert status["developer_mode_env"] is True


class TestDesktopExecution:
    async def test_execute_forwards_mcp_and_env(self, tmp_path, monkeypatch) -> None:
        base = tmp_path / "appdata"
        _write_desktop_config(
            base / "Claude" / "claude_desktop_config.json",
            {
                "mcpServers": {"filesystem": {"command": "npx"}},
                "env": {"ANTHROPIC_BASE_URL": "https://gw.local"},
            },
        )
        monkeypatch.setenv("APPDATA", str(base))

        sdk_manager = MagicMock()
        sdk_manager.execute_command = AsyncMock()
        manager = ClaudeDesktopManager(_settings(), sdk_manager=sdk_manager)
        await manager.execute_command(prompt="hi", working_directory=tmp_path)
        kwargs = sdk_manager.execute_command.await_args.kwargs
        assert kwargs["mcp_servers"] == {"filesystem": {"command": "npx"}}
        assert kwargs["env_overrides"] == {"ANTHROPIC_BASE_URL": "https://gw.local"}
        assert kwargs["prompt"] == "hi"

    async def test_execute_without_config_passes_none(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
        sdk_manager = MagicMock()
        sdk_manager.execute_command = AsyncMock()
        manager = ClaudeDesktopManager(_settings(), sdk_manager=sdk_manager)
        await manager.execute_command(prompt="hi", working_directory=tmp_path)
        kwargs = sdk_manager.execute_command.await_args.kwargs
        assert kwargs["mcp_servers"] is None
        assert kwargs["env_overrides"] is None
