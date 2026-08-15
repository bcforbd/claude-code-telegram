"""Tests for new backend-related commands (agentic + classic)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram import Update
from telegram.ext import ContextTypes

from src.claude.backend import SdkBackend
from src.config.settings import Settings


def _fake_integration():
    """Integration facade stub with a working router + backends."""
    sdk = MagicMock(spec=SdkBackend)
    sdk.name = "sdk"
    sdk.describe.return_value = "sdk"
    sdk.status = AsyncMock(return_value={"ok": True, "backend": "sdk", "model": None})
    desktop = MagicMock()
    desktop.name = "desktop"
    desktop.status = AsyncMock(
        return_value={
            "ok": True,
            "backend": "desktop",
            "config_path": "C:/x/config.json",
            "mcp_servers": ["desktop-commander"],
            "env_keys": ["ANTHROPIC_BASE_URL"],
            "gateway_base_url": "https://gw.local",
            "developer_mode_env": True,
        }
    )
    desktop.mcp_servers.return_value = {"desktop-commander": {"command": "npx"}}
    cowork = MagicMock()
    cowork.name = "cowork"
    cowork.status = AsyncMock(
        return_value={
            "ok": True,
            "backend": "cowork",
            "settings_path": "C:/x/cowork.json",
            "settings_found": True,
            "model": "auto/claude-opus",
            "permission_mode": "acceptEdits",
            "web_search": True,
            "plugin_dirs": [],
            "preferences_loaded": True,
            "preferences_length": 10,
        }
    )
    cowork.mcp_servers.return_value = {}

    integration = MagicMock()
    integration.backends = {"sdk": sdk, "desktop": desktop, "cowork": cowork}
    integration.router = MagicMock()
    integration.router.select.return_value = sdk
    integration.router.available_backends.return_value = ["sdk", "desktop", "cowork"]
    integration.backend_display.side_effect = lambda n: {
        "sdk": "Claude Code (SDK)",
        "desktop": "Claude Desktop mirror",
        "cowork": "Claude Cowork mirror",
    }[n]
    integration.get_backend_status = AsyncMock(
        return_value={
            "active_default": "sdk",
            "available": ["sdk", "desktop", "cowork"],
            "backends": {
                "sdk": sdk.status.return_value,
                "desktop": desktop.status.return_value,
                "cowork": cowork.status.return_value,
            },
        }
    )
    return integration


def _make_context(integration, user_data=None):
    context = MagicMock(spec=ContextTypes)
    context.bot_data = {
        "claude_integration": integration,
        "settings": Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=Path("."),
        ),
        "approval_bridge": None,
    }
    context.user_data = user_data or {}
    return context


def _make_update(text: str):
    update = MagicMock(spec=Update)
    update.effective_user.id = 1
    update.effective_chat.id = 42
    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock()
    update.message = message
    return update


class TestAgenticBackendCommand:
    async def test_show_current(self) -> None:
        integration = _fake_integration()
        update = _make_update("/backend")
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        await orch.agentic_backend(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Backend" in text
        markup = update.message.reply_text.await_args.kwargs.get("reply_markup")
        assert markup is not None
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert any("SDK" in label for label in labels)

    async def test_switch_backend(self) -> None:
        integration = _fake_integration()
        update = _make_update("/backend cowork")
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        await orch.agentic_backend(update, context)
        assert context.user_data["backend"] == "cowork"
        assert context.user_data["claude_session_id"] is None
        text = update.message.reply_text.await_args.args[0]
        assert "Claude Cowork mirror" in text

    async def test_invalid_backend(self) -> None:
        integration = _fake_integration()
        update = _make_update("/backend nope")
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        await orch.agentic_backend(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Unknown backend" in text


class TestAgenticStatusCommands:
    async def test_desktop_status(self) -> None:
        integration = _fake_integration()
        update = _make_update("/desktop")
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        await orch.agentic_desktop(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Claude Desktop" in text
        assert "desktop-commander" in text
        assert "gw.local" in text

    async def test_cowork_status(self) -> None:
        integration = _fake_integration()
        update = _make_update("/cowork")
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        await orch.agentic_cowork(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Claude Cowork" in text
        assert "auto/claude-opus" in text
        assert "acceptEdits" in text


class TestAgenticModelPermission:
    async def test_model_show_and_set(self) -> None:
        integration = _fake_integration()
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        update = _make_update("/model")
        await orch.agentic_model(update, context)
        assert "none" in update.message.reply_text.await_args.args[0]

        update = _make_update("/model claude-sonnet-4-5")
        await orch.agentic_model(update, context)
        assert context.user_data["model_override"] == "claude-sonnet-4-5"

        update = _make_update("/model reset")
        await orch.agentic_model(update, context)
        assert "model_override" not in context.user_data

    async def test_permission_show_and_set(self) -> None:
        integration = _fake_integration()
        context = _make_context(integration)
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        update = _make_update("/permission plan")
        await orch.agentic_permission(update, context)
        assert context.user_data["permission_mode"] == "plan"

        update = _make_update("/permission bogus")
        await orch.agentic_permission(update, context)
        assert "Unknown mode" in update.message.reply_text.await_args.args[0]

    async def test_mcp_listing(self) -> None:
        integration = _fake_integration()
        context = _make_context(integration)
        context.user_data["backend"] = "desktop"
        from src.bot.orchestrator import MessageOrchestrator

        orch = MessageOrchestrator(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=Path("."),
            ),
            {},
        )
        update = _make_update("/mcp")
        await orch.agentic_mcp(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "MCP servers" in text
        assert "desktop-commander" in text


class TestClassicBackendCommands:
    async def test_classic_backend_switch(self) -> None:
        integration = _fake_integration()
        update = _make_update("/backend desktop")
        context = _make_context(integration)
        from src.bot.handlers.command import backend_command

        await backend_command(update, context)
        assert context.user_data["backend"] == "desktop"

    async def test_classic_desktop_status(self) -> None:
        integration = _fake_integration()
        update = _make_update("/desktop")
        context = _make_context(integration)
        from src.bot.handlers.command import desktop_command

        await desktop_command(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "desktop-commander" in text

    async def test_classic_cowork_status(self) -> None:
        integration = _fake_integration()
        update = _make_update("/cowork")
        context = _make_context(integration)
        from src.bot.handlers.command import cowork_command

        await cowork_command(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "auto/claude-opus" in text

    async def test_classic_model(self) -> None:
        integration = _fake_integration()
        context = _make_context(integration)
        from src.bot.handlers.command import model_command

        update = _make_update("/model claude-haiku-4-5")
        await model_command(update, context)
        assert context.user_data["model_override"] == "claude-haiku-4-5"

    async def test_classic_permission(self) -> None:
        integration = _fake_integration()
        context = _make_context(integration)
        from src.bot.handlers.command import permission_command

        update = _make_update("/permission bypassPermissions")
        await permission_command(update, context)
        assert context.user_data["permission_mode"] == "bypassPermissions"

    async def test_classic_mcp(self) -> None:
        integration = _fake_integration()
        update = _make_update("/mcp")
        context = _make_context(integration)
        context.user_data["backend"] = "desktop"
        from src.bot.handlers.command import mcp_command

        await mcp_command(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "desktop-commander" in text
