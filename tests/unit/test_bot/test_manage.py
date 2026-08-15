"""Tests for management commands: plugins/skills/mcp + workflow commands."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.handlers import manage
from src.config import create_test_config


def _make_update(text: str = "/plugins"):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = -1001
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(settings, user_data=None):
    context = MagicMock()
    context.user_data = user_data or {}
    context.bot_data = {"settings": settings}
    return context


def _query_update(data: str):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


# ---------------------------------------------------------------------------
# /plugins management
# ---------------------------------------------------------------------------


async def test_plugins_install_runs_cli(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/plugins install demo-plugin")
    context = _make_context(settings)
    with patch.object(
        manage, "run_claude_cli", return_value=(0, "installed demo-plugin")
    ) as m:
        await manage.plugins_manage(update, context)
    m.assert_called_once_with(["plugin", "install", "-y", "demo-plugin"], settings)
    assert "installed demo-plugin" in update.message.reply_text.call_args.args[0]


async def test_plugins_remove_asks_confirmation(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/plugins remove demo-plugin")
    context = _make_context(settings)
    await manage.plugins_manage(update, context)
    kwargs = update.message.reply_text.call_args.kwargs
    data = [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert any(d.startswith("mng:confirm:uninstall:") for d in data)
    assert "cancel:mng" in data


async def test_plugins_marketplace_add(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/plugins marketplace add https://example.com/repo")
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(0, "ok")) as m:
        await manage.plugins_manage(update, context)
    m.assert_called_once_with(
        ["plugin", "marketplace", "add", "https://example.com/repo"], settings
    )


async def test_mng_confirm_runs_plugin_op(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update, query = _query_update("mng:confirm:uninstall:demo-plugin")
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(0, "uninstalled")):
        await manage.mng_confirm(update, context)
    text = query.edit_message_text.call_args.args[0]
    assert "uninstalled" in text


async def test_mng_confirm_skill_delete(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(tmp_path / "claude"),
    )
    skill_dir = tmp_path / "claude" / "skills" / "old-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# old\n", encoding="utf-8")

    update, query = _query_update("mng:confirm:skill_delete:old-skill")
    context = _make_context(settings)
    await manage.mng_confirm(update, context)

    assert not skill_dir.exists()
    assert "Deleted skill" in query.edit_message_text.call_args.args[0]


# ---------------------------------------------------------------------------
# /skills management
# ---------------------------------------------------------------------------


async def test_skills_create_scaffolds(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(tmp_path / "claude"),
    )
    update = _make_update("/skills create my-skill Does things")
    context = _make_context(settings)
    await manage.skills_manage(update, context)
    md = tmp_path / "claude" / "skills" / "my-skill" / "SKILL.md"
    assert md.is_file()
    content = md.read_text(encoding="utf-8")
    assert "name: my-skill" in content
    assert "description: Does things" in content
    assert "Created skill" in update.message.reply_text.call_args.args[0]


async def test_skills_create_rejects_bad_name(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(tmp_path / "claude"),
    )
    update = _make_update("/skills create 'bad name!'")
    context = _make_context(settings)
    await manage.skills_manage(update, context)
    assert "Invalid skill name" in update.message.reply_text.call_args.args[0]


async def test_skills_show_prints_skill(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(tmp_path / "claude"),
    )
    d = tmp_path / "claude" / "skills" / "paperclip"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# Paperclip\n\nDo agent things.\n", encoding="utf-8")
    update = _make_update("/skills show paperclip")
    context = _make_context(settings)
    await manage.skills_manage(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "Paperclip" in text
    assert "Do agent things" in text


async def test_skills_delete_asks_confirmation(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(tmp_path / "claude"),
    )
    d = tmp_path / "claude" / "skills" / "old"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# old\n", encoding="utf-8")
    update = _make_update("/skills delete old")
    context = _make_context(settings)
    await manage.skills_manage(update, context)
    kwargs = update.message.reply_text.call_args.kwargs
    data = [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert any(d.startswith("mng:confirm:skill_delete:") for d in data)


# ---------------------------------------------------------------------------
# /mcp management
# ---------------------------------------------------------------------------


async def test_mcp_add_writes_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"seed": {"command": "echo", "args": []}}}),
        encoding="utf-8",
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        mcp_config_path=str(cfg),
    )
    update = _make_update("/mcp add mydb python server.py")
    context = _make_context(settings)
    await manage.mcp_manage(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["mydb"] == {
        "command": "python",
        "args": ["server.py"],
        "enabled": True,
    }


async def test_mcp_remove_writes_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"mydb": {"command": "python", "args": []}}}),
        encoding="utf-8",
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        mcp_config_path=str(cfg),
    )
    update = _make_update("/mcp remove mydb")
    context = _make_context(settings)
    await manage.mcp_manage(update, context)
    # asks for confirmation first
    kwargs = update.message.reply_text.call_args.kwargs
    data = [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert any(d.startswith("mng:confirm:mcp_remove:") for d in data)
    # confirm deletes
    query = MagicMock()
    query.data = "mng:confirm:mcp_remove:mydb"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update2 = MagicMock()
    update2.callback_query = query
    with patch.object(manage, "run_claude_cli", return_value=(0, "")):
        await manage.mcp_confirm_remove(update2, context)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mydb" not in data["mcpServers"]


# ---------------------------------------------------------------------------
# MCP system: transports, enable/disable, connect, OAuth
# ---------------------------------------------------------------------------


def _mcp_settings(tmp_path, servers=None):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {"mcpServers": servers or {"seed": {"command": "echo", "args": []}}}
        ),
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        mcp_config_path=str(cfg),
    )


async def test_mcp_add_sse_writes_transport(tmp_path):
    settings = _mcp_settings(tmp_path)
    update = _make_update(
        "/mcp add-sse events https://sse.example.com/mcp X-Test: hello"
    )
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(0, "ok")):
        await manage.mcp_manage(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["events"]
    assert entry["type"] == "sse"
    assert entry["url"] == "https://sse.example.com/mcp"
    assert entry["headers"] == {"X-Test": "hello"}


async def test_mcp_add_http_streamable(tmp_path):
    settings = _mcp_settings(tmp_path)
    update = _make_update("/mcp add-http tools https://mcp.example.com/mcp")
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(0, "ok")):
        await manage.mcp_manage(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["tools"]["type"] == "http"


async def test_mcp_disable_enable_toggles(tmp_path):
    settings = _mcp_settings(tmp_path, {"mydb": {"command": "python", "args": []}})
    update = _make_update("/mcp disable mydb")
    context = _make_context(settings)
    await manage.mcp_manage(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["mydb"]["enabled"] is False

    update2 = _make_update("/mcp enable mydb")
    await manage.mcp_manage(update2, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["mydb"]["enabled"] is True


async def test_mcp_set_custom_json(tmp_path):
    settings = _mcp_settings(tmp_path)
    update = _make_update(
        '/mcp set fancy {"type": "http", "url": "https://x/y", "enabled": true}'
    )
    context = _make_context(settings)
    await manage.mcp_manage(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fancy"]["type"] == "http"


async def test_mcp_connect_stdio_checks_command(tmp_path):
    settings = _mcp_settings(tmp_path, {"echo": {"command": "echo", "args": []}})
    update = _make_update("/mcp connect echo")
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(1, "not in user config")):
        await manage.mcp_manage(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "echo" in text
    assert "found" in text.lower() or "not found" in text.lower()


async def test_mcp_auth_starts_login_and_prints_url(tmp_path):
    settings = _mcp_settings(
        tmp_path, {"github": {"type": "http", "url": "https://mcp.example.com"}}
    )
    update = _make_update("/mcp auth github")
    context = _make_context(settings)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.kill = MagicMock()
    with (
        patch.object(manage, "start_mcp_login", return_value=proc) as start,
        patch.object(
            manage,
            "poll_login_url",
            return_value="https://auth.example.com/authorize?client=1",
        ),
        patch.object(manage, "asyncio_get_event_loop") as loop,
    ):
        loop.return_value.run_in_executor = AsyncMock(
            return_value="https://auth.example.com/authorize?client=1"
        )
        await manage.mcp_auth(update, context, "github")
    start.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "https://auth.example.com/authorize?client=1" in text
    assert "callback" in text.lower()


async def test_mcp_finish_login_feeds_redirect_url(tmp_path):
    settings = _mcp_settings(tmp_path)
    update = _make_update("/mcp callback http://localhost:1234/callback?code=xyz")
    context = _make_context(settings)
    proc = MagicMock()
    proc.poll.side_effect = [None, 0]  # running at lookup, done after write
    proc.returncode = 0
    proc.stdin = MagicMock()
    out_path = tmp_path / "login.log"
    out_path.write_text(
        "Please visit: https://auth.example.com\nToken saved.\n", encoding="utf-8"
    )
    manage._login_registry(context)["github"] = {"proc": proc, "out": out_path}
    await manage.mcp_finish_login(
        update, context, "http://localhost:1234/callback?code=xyz"
    )
    proc.stdin.write.assert_called_once()
    assert "Auth completed" in update.message.reply_text.call_args.args[0]


async def test_mcp_button_toggle(tmp_path):
    settings = _mcp_settings(tmp_path, {"mydb": {"command": "python", "args": []}})
    query = MagicMock()
    query.data = "mng:mcp:toggle:mydb"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = _make_context(settings)
    await manage.mcp_button_callback(update, context)
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["mydb"]["enabled"] is False


async def test_plugins_enable_disable(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/plugins enable superpowers")
    context = _make_context(settings)
    with patch.object(manage, "run_claude_cli", return_value=(0, "enabled")) as m:
        await manage.plugins_manage(update, context)
    m.assert_called_once_with(["plugin", "enable", "superpowers"], settings)

    update2 = _make_update("/plugins disable superpowers")
    with patch.object(manage, "run_claude_cli", return_value=(0, "disabled")) as m2:
        await manage.plugins_manage(update2, context)
    m2.assert_called_once_with(["plugin", "disable", "superpowers"], settings)


# ---------------------------------------------------------------------------
# Workflow commands
# ---------------------------------------------------------------------------


async def test_goal_set_clear(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    context = _make_context(settings)
    update = _make_update("/goal build the thing")
    await manage.goal_command(update, context)
    assert context.user_data["goal"] == "build the thing"

    update2 = _make_update("/goal off")
    await manage.goal_command(update2, context)
    assert "goal" not in context.user_data


async def test_loop_set_off(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    context = _make_context(settings)
    update = _make_update("/loop 5")
    await manage.loop_command(update, context)
    assert context.user_data["loop_remaining"] == 5

    update2 = _make_update("/loop off")
    await manage.loop_command(update2, context)
    assert "loop_remaining" not in context.user_data


async def test_clear_resets_session_state(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    context = _make_context(
        settings, {"claude_session_id": "s1", "goal": "g", "loop_remaining": 2}
    )
    update = _make_update("/clear")
    await manage.clear_command(update, context)
    assert context.user_data["claude_session_id"] is None
    assert context.user_data["force_new_session"] is True
    assert "goal" not in context.user_data


async def test_todo_add_done_clear(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    context = _make_context(settings)
    await manage.todo_command(_make_update("/todo add first"), context)
    await manage.todo_command(_make_update("/todo add second"), context)
    assert context.user_data["todos"] == ["first", "second"]
    await manage.todo_command(_make_update("/todo done 1"), context)
    assert context.user_data["todos"] == ["second"]
    await manage.todo_command(_make_update("/todo clear"), context)
    assert context.user_data["todos"] == []


async def test_plan_sets_permission(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    context = _make_context(settings)
    await manage.plan_command(_make_update("/plan"), context)
    assert context.user_data["permission_mode"] == "plan"
    await manage.plan_command(_make_update("/plan off"), context)
    assert "permission_mode" not in context.user_data


async def test_config_command_shows_settings(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/config")
    context = _make_context(settings)
    await manage.config_command(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "agentic" in text
    assert "Approved dir" in text


async def test_memory_command_shows_claude_md(tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / "CLAUDE.md").write_text("# Memory\n\nImportant facts.\n", encoding="utf-8")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/memory")
    context = _make_context(settings)
    await manage.memory_command(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "Important facts" in text


async def test_hooks_command_lists(tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": "echo done"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/hooks")
    context = _make_context(settings)
    await manage.hooks_command(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "Stop" in text
    assert "echo done" in text


async def test_compact_requires_session(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/compact")
    context = _make_context(settings)
    await manage.compact_command(update, context)
    assert "No active session" in update.message.reply_text.call_args.args[0]


async def test_review_requires_git(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/review")
    context = _make_context(settings)
    with patch("subprocess.run", return_value=MagicMock(returncode=128)):
        await manage.review_command(update, context)
    assert "not a git repository" in update.message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# Cancel buttons
# ---------------------------------------------------------------------------


async def test_cancel_callback_removes_keyboard(tmp_path):
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    update, query = _query_update("cancel:backend")
    context = _make_context(settings)
    await orchestrator._handle_cancel_callback(update, context)
    query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)


async def test_cancel_mng_replaces_text(tmp_path):
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    update, query = _query_update("cancel:mng")
    context = _make_context(settings)
    await orchestrator._handle_cancel_callback(update, context)
    assert "Cancelled" in query.edit_message_text.call_args.args[0]


async def test_page_keyboard_has_cancel():
    from src.bot.handlers.capabilities import _page_keyboard

    kb = _page_keyboard("skills", 0, 3, "")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "cancel:skills" in data


async def test_backend_buttons_have_cancel(tmp_path):
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    update = _make_update("/backend")
    context = _make_context(settings)
    integration = MagicMock()
    integration.router.available_backends.return_value = ["sdk", "desktop", "cowork"]
    integration.router.select.return_value = MagicMock(name="sdk")
    integration.backend_display.side_effect = lambda n: n
    context.bot_data["claude_integration"] = integration
    await orchestrator.agentic_backend(update, context)
    kwargs = update.message.reply_text.call_args.kwargs
    data = [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert "cancel:backend" in data


async def test_maybe_loop_schedules_job(tmp_path):
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    job_queue = MagicMock()
    context = _make_context(settings, {"loop_remaining": 2})
    context.job_queue = job_queue
    context.chat_id = 123
    context.user_id = 1

    await orchestrator._maybe_loop(context)

    assert context.user_data["loop_remaining"] == 1
    job_queue.run_once.assert_called_once()
    # exhausted -> no more scheduling
    await orchestrator._maybe_loop(context)
    assert "loop_remaining" not in context.user_data
    assert job_queue.run_once.call_count == 1


def test_parse_headers_recombines_multiword_values():
    headers = manage._parse_headers(
        ["X-Test:", "hello", "world", "Authorization:", "Bearer", "tok"]
    )
    assert headers == {"X-Test": "hello world", "Authorization": "Bearer tok"}


def test_parse_headers_single_token():
    assert manage._parse_headers(["X-Api-Key:abc"]) == {"X-Api-Key": "abc"}
