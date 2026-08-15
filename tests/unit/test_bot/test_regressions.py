"""Regression tests for bug fixes: approvals deadlock, session export, quick actions."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update

from src.bot.update_processor import StopAwareUpdateProcessor

# ---------------------------------------------------------------------------
# B1: approvals must bypass the sequential lock (deadlock fix)
# ---------------------------------------------------------------------------


def _update_with_callback(data: str) -> Update:
    update = MagicMock(spec=Update)
    cb = MagicMock()
    cb.data = data
    update.callback_query = cb
    return update


def test_approve_callback_is_priority():
    proc = StopAwareUpdateProcessor()
    assert proc._is_priority_callback(_update_with_callback("approve:abc:allow"))
    assert proc._is_priority_callback(_update_with_callback("stop:123"))


def test_regular_callbacks_not_priority():
    proc = StopAwareUpdateProcessor()
    assert not proc._is_priority_callback(_update_with_callback("cd:/proj"))
    assert not proc._is_priority_callback(_update_with_callback("sess:abc"))
    # Not an Update instance at all
    assert not proc._is_priority_callback(MagicMock())
    # Update without a callback query
    update = MagicMock(spec=Update)
    update.callback_query = None
    assert not proc._is_priority_callback(update)


async def test_approve_callback_runs_without_lock():
    """approve: callbacks must NOT wait for the sequential lock."""
    proc = StopAwareUpdateProcessor()
    ran = []

    async def coro():
        ran.append("ran")

    update = _update_with_callback("approve:req1:allow")
    await proc.do_process_update(update, coro())
    assert ran == ["ran"]


async def test_normal_update_acquires_lock():
    """Non-priority updates still serialize through the lock."""
    proc = StopAwareUpdateProcessor()
    ran = []

    async def coro():
        ran.append("ran")

    await proc.do_process_update(MagicMock(spec=Update), coro())
    assert ran == ["ran"]


# ---------------------------------------------------------------------------
# B2: SessionExporter uses the real storage API (get_session_history)
# ---------------------------------------------------------------------------


@pytest.fixture
def export_storage():
    storage = MagicMock()
    storage.get_session_history = AsyncMock(
        return_value={
            "session": {
                "session_id": "abc-123",
                "user_id": 123,
                "project_path": "C:/proj",
                "created_at": "2026-08-14T00:00:00",
                "last_used": "2026-08-14T10:00:00",
                "total_cost": 0.42,
                "total_turns": 3,
                "message_count": 1,
                "is_active": True,
            },
            "messages": [
                {
                    "session_id": "abc-123",
                    "user_id": 123,
                    "timestamp": "2026-08-14T10:00:00",
                    "prompt": "hello",
                    "response": "hi there",
                }
            ],
            "tool_usage": [],
        }
    )
    return storage


async def test_exporter_uses_get_session_history(export_storage):
    from src.bot.features.session_export import ExportFormat, SessionExporter

    exporter = SessionExporter(export_storage)
    result = await exporter.export_session(123, "abc-123", ExportFormat.MARKDOWN)

    export_storage.get_session_history.assert_awaited_once()
    assert result.filename.endswith(".md")
    assert "hello" in result.content
    assert "hi there" in result.content
    assert "abc-123" in result.content


async def test_exporter_json_format(export_storage):
    from src.bot.features.session_export import ExportFormat, SessionExporter

    exporter = SessionExporter(export_storage)
    result = await exporter.export_session(123, "abc-123", ExportFormat.JSON)

    assert result.filename.endswith(".json")
    assert '"role": "user"' in result.content
    assert '"role": "assistant"' in result.content


async def test_exporter_rejects_foreign_session(export_storage):
    from src.bot.features.session_export import ExportFormat, SessionExporter

    exporter = SessionExporter(export_storage)
    with pytest.raises(ValueError):
        await exporter.export_session(999, "abc-123", ExportFormat.MARKDOWN)


async def test_exporter_missing_session():
    storage = MagicMock()
    storage.get_session_history = AsyncMock(return_value=None)
    from src.bot.features.session_export import ExportFormat, SessionExporter

    exporter = SessionExporter(storage)
    with pytest.raises(ValueError):
        await exporter.export_session(123, "nope", ExportFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# B3: quick actions keyboard prefix + command field
# ---------------------------------------------------------------------------


def test_quick_action_keyboard_uses_quick_prefix():
    from src.bot.features.quick_actions import QuickActionManager

    manager = QuickActionManager()
    keyboard = manager.create_inline_keyboard([manager.actions["test"]])
    data = keyboard.inline_keyboard[0][0].callback_data
    assert data == "quick:test"


async def test_quick_action_execute_returns_command():
    from src.bot.features.quick_actions import QuickActionManager

    manager = QuickActionManager()
    assert await manager.execute_action("test", MagicMock()) == "test"


# ---------------------------------------------------------------------------
# B6: claude_response initialized before run (no NameError masking)
# ---------------------------------------------------------------------------


def test_message_handler_binds_claude_response_before_run():
    """The classic text handler initializes claude_response = None so an
    exception inside run_command cannot cause a NameError later."""
    src = open(
        Path(__file__).resolve().parents[3] / "src" / "bot" / "handlers" / "message.py",
        encoding="utf-8",
    ).read()
    assert "claude_response = None" in src
