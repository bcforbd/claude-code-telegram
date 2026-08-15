"""Tests for shared insight commands: stop, cost, stats, sessions, context, jobs, doctor."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.handlers.insights import (
    context_command,
    cost_command,
    doctor_command,
    handle_sessions_callback,
    jobs_command,
    sessions_command,
    stats_command,
    stop_command,
)


def _make_update(text: str = "/cost"):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = -1001
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    return context


# ---------------------------------------------------------------------------
# /stop
# ---------------------------------------------------------------------------


async def test_stop_command_interrupts_active_request():
    update = _make_update("/stop")
    context = _make_context()
    orchestrator = MagicMock()
    orchestrator.interrupt_user = AsyncMock(return_value=True)
    context.bot_data["orchestrator"] = orchestrator

    await stop_command(update, context)

    orchestrator.interrupt_user.assert_awaited_once_with(123)
    update.message.reply_text.assert_called_once()
    assert "Stopping" in update.message.reply_text.call_args[0][0]


async def test_stop_command_no_active_request():
    update = _make_update("/stop")
    context = _make_context()
    orchestrator = MagicMock()
    orchestrator.interrupt_user = AsyncMock(return_value=False)
    context.bot_data["orchestrator"] = orchestrator

    await stop_command(update, context)

    assert "No active request" in update.message.reply_text.call_args[0][0]


async def test_stop_command_without_orchestrator():
    update = _make_update("/stop")
    context = _make_context()

    await stop_command(update, context)

    assert "Unavailable" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# /cost
# ---------------------------------------------------------------------------


async def test_cost_command_shows_real_spend():
    update = _make_update("/cost")
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "s1",
                "project_path": "C:/proj",
                "total_cost": 0.42,
                "message_count": 5,
            },
            {
                "session_id": "s2",
                "project_path": "C:/other",
                "total_cost": 0.10,
                "message_count": 2,
            },
        ]
    )
    integration.get_active_backend_name.return_value = "cowork"
    integration.backend_display.return_value = "Claude Cowork mirror"
    context.bot_data["claude_integration"] = integration
    context.user_data["claude_session_id"] = "s1"

    rate_limiter = MagicMock()
    rate_limiter.get_user_status.return_value = {
        "cost_usage": {
            "current": 0.52,
            "limit": 10.0,
            "remaining": 9.48,
            "utilization": 0.052,
        }
    }
    context.bot_data["rate_limiter"] = rate_limiter

    await cost_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "$0.4200" in msg  # current session cost
    assert "$0.5200" in msg  # total across sessions
    assert "$9.48" in msg  # budget remaining
    assert "Cowork" in msg


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------


async def test_stats_command_shows_dashboard():
    update = _make_update("/stats")
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "s1",
                "total_cost": 0.5,
                "message_count": 3,
            }
        ]
    )
    storage = MagicMock()
    storage.get_user_dashboard = AsyncMock(
        return_value={
            "user": {"last_active": "2026-08-14T00:00:00Z"},
            "daily_costs": [
                {"date": "2026-08-13", "total_cost": 0.2},
                {"date": "2026-08-14", "total_cost": 0.3},
            ],
        }
    )
    context.bot_data["claude_integration"] = integration
    context.bot_data["storage"] = storage
    rate_limiter = MagicMock()
    rate_limiter.get_user_status.return_value = {
        "request_bucket": {"tokens": 5, "capacity": 10}
    }
    context.bot_data["rate_limiter"] = rate_limiter

    await stats_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "Sessions:" in msg
    assert "$0.5000" in msg
    assert "2026-08-14" in msg
    assert "5/10" in msg


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------


async def test_sessions_command_lists_with_buttons():
    update = _make_update("/sessions")
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "abc-123",
                "project_path": "C:/proj",
                "total_cost": 0.42,
                "message_count": 5,
                "last_used": "2026-08-14T10:00:00",
            }
        ]
    )
    context.bot_data["claude_integration"] = integration
    context.user_data["claude_session_id"] = "other"

    await sessions_command(update, context)

    update.message.reply_text.assert_called_once()
    kwargs = update.message.reply_text.call_args.kwargs
    assert kwargs["reply_markup"] is not None
    buttons = kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "sess:abc-123"


async def test_sessions_command_empty():
    update = _make_update("/sessions")
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(return_value=[])
    context.bot_data["claude_integration"] = integration

    await sessions_command(update, context)

    assert "No sessions" in update.message.reply_text.call_args[0][0]


async def test_sessions_callback_switches_session():
    query = MagicMock()
    query.from_user.id = 123
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "abc-123",
                "project_path": "C:/proj",
                "total_cost": 0.42,
                "message_count": 5,
            }
        ]
    )
    context.bot_data["claude_integration"] = integration

    await handle_sessions_callback(query, "abc-123", context)

    assert context.user_data["claude_session_id"] == "abc-123"
    assert context.user_data["current_directory"] == Path("C:/proj")
    query.answer.assert_called_once()


async def test_sessions_callback_unknown_session():
    query = MagicMock()
    query.from_user.id = 123
    query.answer = AsyncMock()
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(return_value=[])
    context.bot_data["claude_integration"] = integration

    await handle_sessions_callback(query, "nope", context)

    query.answer.assert_called_once()
    assert "not found" in query.answer.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# /context
# ---------------------------------------------------------------------------


async def test_context_command_no_session():
    update = _make_update("/context")
    context = _make_context()
    context.bot_data["claude_integration"] = MagicMock()

    await context_command(update, context)

    assert "No active session" in update.message.reply_text.call_args[0][0]


async def test_context_command_with_session():
    update = _make_update("/context")
    context = _make_context()
    integration = MagicMock()
    integration.get_user_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "abc-123",
                "project_path": "C:/proj",
                "total_cost": 0.42,
                "message_count": 5,
                "created_at": "2026-08-14T00:00:00",
                "last_used": "2026-08-14T10:00:00",
            }
        ]
    )
    storage = MagicMock()
    storage.get_session_history = AsyncMock(
        return_value={
            "messages": [{"prompt": "hi"}, {"response": "yo"}],
            "tool_usage": [{"tool_name": "Bash"}],
        }
    )
    context.bot_data["claude_integration"] = integration
    context.bot_data["storage"] = storage
    context.user_data["claude_session_id"] = "abc-123"

    await context_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "proj" in msg
    assert "abc-123" in msg
    assert "Bash" in msg
    assert "2" in msg  # history rows


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------


async def test_jobs_command_scheduler_disabled():
    update = _make_update("/jobs")
    context = _make_context()

    await jobs_command(update, context)

    assert "Scheduler disabled" in update.message.reply_text.call_args[0][0]


async def test_jobs_command_lists_jobs():
    update = _make_update("/jobs")
    context = _make_context()
    scheduler = MagicMock()
    scheduler.list_jobs = AsyncMock(
        return_value=[
            {
                "job_id": "job-1",
                "job_name": "morning-tests",
                "cron_expression": "0 9 * * 1-5",
                "prompt": "run tests",
            }
        ]
    )
    context.bot_data["scheduler"] = scheduler

    await jobs_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "morning-tests" in msg
    assert "0 9 * * 1-5" in msg


async def test_jobs_command_add_valid():
    update = _make_update('/jobs add myjob "0 9 * * *" run tests and report')
    context = _make_context()
    scheduler = MagicMock()
    scheduler.add_job = AsyncMock(return_value="job-42")
    context.bot_data["scheduler"] = scheduler
    settings = MagicMock()
    settings.approved_directory = Path("C:/base")
    context.bot_data["settings"] = settings

    await jobs_command(update, context)

    scheduler.add_job.assert_awaited_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["job_name"] == "myjob"
    assert kwargs["cron_expression"] == "0 9 * * *"
    assert kwargs["prompt"] == "run tests and report"
    msg = update.message.reply_text.call_args[0][0]
    assert "job-42" in msg


async def test_jobs_command_add_invalid_cron():
    update = _make_update("/jobs add bad 'not a cron' do things")
    context = _make_context()
    scheduler = MagicMock()
    context.bot_data["scheduler"] = scheduler

    await jobs_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "Invalid cron" in msg
    scheduler.add_job.assert_not_called()


async def test_jobs_command_remove():
    update = _make_update("/jobs remove job-1")
    context = _make_context()
    scheduler = MagicMock()
    scheduler.remove_job = AsyncMock(return_value=True)
    context.bot_data["scheduler"] = scheduler

    await jobs_command(update, context)

    scheduler.remove_job.assert_awaited_once_with("job-1")
    assert "job-1" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# /doctor
# ---------------------------------------------------------------------------


async def test_doctor_command_runs_checks():
    update = _make_update("/doctor")
    context = _make_context()
    settings = MagicMock()
    settings.allowed_users = [123]
    settings.anthropic_api_key = None
    context.bot_data["settings"] = settings

    integration = MagicMock()
    integration.get_backend_status = AsyncMock(
        return_value={
            "active_default": "sdk",
            "available": ["sdk", "desktop", "cowork"],
            "backends": {
                "sdk": {"description": "Claude Code (SDK)", "mcp_servers": []},
                "desktop": {
                    "description": "Claude Desktop mirror",
                    "mcp_servers": ["desktop-commander", "telegram"],
                },
                "cowork": {"description": "Claude Cowork mirror"},
            },
        }
    )
    context.bot_data["claude_integration"] = integration

    storage = MagicMock()
    storage.health_check = AsyncMock(return_value=True)
    context.bot_data["storage"] = storage

    bridge = MagicMock()
    bridge.pending_count.return_value = 0
    bridge.always_allow_all.return_value = {123: {"Bash"}}
    context.bot_data["approval_bridge"] = bridge

    with patch("src.bot.handlers.insights.shutil.which", return_value="C:/claude.exe"):
        await doctor_command(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "Doctor Report" in msg
    assert "desktop-commander" in msg
    assert "Claude CLI" in msg


# ---------------------------------------------------------------------------
# stop callback still works via orchestrator
# ---------------------------------------------------------------------------


async def test_orchestrator_interrupt_user():
    from src.bot.orchestrator import ActiveRequest, MessageOrchestrator

    orchestrator = MessageOrchestrator.__new__(MessageOrchestrator)
    orchestrator._active_requests = {}
    event = asyncio.Event()
    orchestrator._active_requests[7] = ActiveRequest(
        user_id=7,
        interrupt_event=event,
        interrupted=False,
        progress_msg=MagicMock(),
    )

    assert await orchestrator.interrupt_user(7) is True
    assert event.is_set()

    # Second call: already interrupted
    assert await orchestrator.interrupt_user(7) is False

    # Unknown user
    assert await orchestrator.interrupt_user(99) is False
