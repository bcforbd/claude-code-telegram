"""Tests for command menu scopes and the /help command."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import BotCommand, Update

from src.bot.core import ClaudeCodeBot
from src.config import create_test_config


def _make_context(settings):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": settings}
    return context


def _make_update(text: str = "/help"):
    update = MagicMock(spec=Update)
    update.effective_user.id = 123
    update.effective_chat.id = -1001
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


# ---------------------------------------------------------------------------
# Menu scopes
# ---------------------------------------------------------------------------


async def test_set_bot_commands_covers_all_scopes(tmp_path):
    """The full command list is pushed to default + all chat scopes.

    A stale 3-command all_private_chats scope (left by an older bot version)
    shadows the default scope, so every scope must be refreshed.
    """
    core = object.__new__(ClaudeCodeBot)
    core.orchestrator = MagicMock()
    core.orchestrator.get_bot_commands = AsyncMock(
        return_value=[BotCommand("start", "Start"), BotCommand("help", "Help")]
    )
    core.app = MagicMock()
    core.app.bot.set_my_commands = AsyncMock()

    await core._set_bot_commands()

    assert core.app.bot.set_my_commands.await_count == 4
    scopes = [
        call.kwargs["scope"] for call in core.app.bot.set_my_commands.await_args_list
    ]
    types = {type(s).__name__ for s in scopes}
    assert types == {
        "BotCommandScopeDefault",
        "BotCommandScopeAllPrivateChats",
        "BotCommandScopeAllGroupChats",
        "BotCommandScopeAllChatAdministrators",
    }
    for call in core.app.bot.set_my_commands.await_args_list:
        assert len(call.args[0]) == 2


async def test_agentic_menu_has_all_commands(tmp_path):
    """Agentic menu exposes every command that has a handler."""
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator.register_handlers(MagicMock())  # populates _known_commands
    commands = await orchestrator.get_bot_commands()
    assert len(commands) == 42
    assert {c.command for c in commands} == orchestrator._known_commands


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agentic", [True, False])
async def test_help_lists_every_menu_command(tmp_path, agentic):
    """/help mentions every command registered in the menu."""
    from src.bot.handlers.command import help_command
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(
        approved_directory=str(tmp_path), agentic_mode=agentic
    )
    orchestrator = MessageOrchestrator(settings, {})
    menu = await orchestrator.get_bot_commands()

    update = _make_update("/help")
    context = _make_context(settings)
    await help_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert len(text) < 4096
    for cmd in menu:
        assert f"/{cmd.command}" in text


async def test_help_hides_agentic_only_commands_in_classic(tmp_path):
    """Classic /help does not advertise agentic-only commands."""
    from src.bot.handlers.command import help_command

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=False)
    update = _make_update("/help")
    context = _make_context(settings)
    await help_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "/restart" in text  # restart IS in the classic menu
    assert "/repo" not in text
    assert "/verbose" not in text
