"""Tests for capability commands (/plugins, /skills, /features) + backend buttons."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers.capabilities import (
    collect_mcp_servers,
    features_command,
    plugins_command,
    skills_command,
)
from src.config import create_test_config


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at tmp_path so tests never read real user config."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _make_update(text: str = "/features"):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = -1001
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(settings):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": settings}
    return context


def _fake_integration():
    """Integration facade stub with a working router + backends."""
    sdk = MagicMock()
    sdk.name = "sdk"
    desktop = MagicMock()
    desktop.name = "desktop"
    cowork = MagicMock()
    cowork.name = "cowork"
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
    return integration


# ---------------------------------------------------------------------------
# /backend buttons (agentic)
# ---------------------------------------------------------------------------


async def test_agentic_backend_shows_buttons(tmp_path):
    """/backend without args shows one button per backend with backend:set data."""
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    update = _make_update("/backend")
    context = _make_context(settings)
    context.bot_data["claude_integration"] = _fake_integration()

    await orchestrator.agentic_backend(update, context)

    update.message.reply_text.assert_called_once()
    kwargs = update.message.reply_text.call_args.kwargs
    markup = kwargs.get("reply_markup")
    assert markup is not None
    buttons = [
        btn
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data and btn.callback_data.startswith("backend:set:")
    ]
    assert {b.callback_data.split(":", 2)[2] for b in buttons} == {
        "sdk",
        "desktop",
        "cowork",
    }


async def test_backend_callback_switches_backend(tmp_path):
    """backend:set:<name> callback switches backend and resets the session."""
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    query = MagicMock()
    query.data = "backend:set:desktop"
    query.from_user.id = 123
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = _make_context(settings)
    context.bot_data["claude_integration"] = _fake_integration()
    context.bot_data["audit_logger"] = None
    context.user_data["claude_session_id"] = "old-session"

    await orchestrator._handle_backend_callback(update, context)

    assert context.user_data["backend"] == "desktop"
    assert context.user_data["claude_session_id"] is None
    assert context.user_data["force_new_session"] is True
    query.edit_message_text.assert_called_once()
    assert "Desktop" in query.edit_message_text.call_args.args[0]


async def test_backend_callback_rejects_unknown(tmp_path):
    """Unknown backend name is rejected with an error message."""
    from src.bot.orchestrator import MessageOrchestrator

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    orchestrator = MessageOrchestrator(settings, {})
    query = MagicMock()
    query.data = "backend:set:quantum"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = _make_context(settings)
    context.bot_data["claude_integration"] = _fake_integration()

    await orchestrator._handle_backend_callback(update, context)

    assert "backend" not in context.user_data
    assert "Unknown backend" in query.edit_message_text.call_args.args[0]


# ---------------------------------------------------------------------------
# /plugins
# ---------------------------------------------------------------------------


def _make_plugin_dir(root: Path, name: str) -> Path:
    plugin = root / name / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8"
    )
    return root / name


async def test_plugins_command_lists_discovered(tmp_path):
    """/plugins lists plugin folders found in configured plugin dirs."""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    _make_plugin_dir(plugins_root, "demo-plugin")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        cowork_plugin_dirs=[str(plugins_root)],
    )
    update = _make_update("/plugins")
    context = _make_context(settings)

    await plugins_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "demo-plugin" in text
    assert "Discovered" in text


async def test_plugins_command_empty_hint(tmp_path):
    """/plugins with no configured dirs shows a setup hint."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    update = _make_update("/plugins")
    context = _make_context(settings)

    await plugins_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "COWORK_PLUGIN_DIRS" in text


async def test_plugins_command_lists_installed(tmp_path):
    """/plugins lists real installed plugins from the Claude config dir."""
    cfg = tmp_path / "claude"
    plugins_dir = cfg / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "frontend-design@claude-plugins-official": [{"version": "unknown"}],
                    "superpowers@claude-plugins-official": [{"version": "6.3.0"}],
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
    update = _make_update("/plugins")
    context = _make_context(settings)

    await plugins_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "frontend-design" in text
    assert "superpowers" in text
    assert "v6.3.0" in text
    assert "Installed" in text


# ---------------------------------------------------------------------------
# MCP aggregation
# ---------------------------------------------------------------------------


def _write_settings_json(cfg: Path, servers: dict) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def test_collect_mcp_servers_aggregates_sources(tmp_path, fake_home):
    """MCP servers come from Claude Code settings.json + Claude Desktop."""
    cfg = tmp_path / "claude"
    _write_settings_json(cfg, {"telegram": {"command": "bun"}})
    desktop_cfg = (
        tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    )
    desktop_cfg.parent.mkdir(parents=True)
    desktop_cfg.write_text(
        json.dumps({"mcpServers": {"desktop-commander": {"command": "npx"}}}),
        encoding="utf-8",
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )

    sources = collect_mcp_servers(settings)

    labels = [label for label, _ in sources]
    assert "Claude Code settings.json" in labels
    assert "Claude Desktop" in labels
    by_label = dict(sources)
    assert by_label["Claude Code settings.json"] == ["telegram"]
    assert by_label["Claude Desktop"] == ["desktop-commander"]


def test_collect_mcp_servers_empty(tmp_path, fake_home):
    """No sources -> no aggregated MCP servers."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    assert collect_mcp_servers(settings) == []


async def test_agentic_mcp_shows_config_dir_servers(tmp_path, fake_home):
    """/mcp (agentic) shows real servers from the Claude config dir."""
    from src.bot.orchestrator import MessageOrchestrator

    cfg = tmp_path / "claude"
    _write_settings_json(cfg, {"telegram": {"command": "bun"}})
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    orchestrator = MessageOrchestrator(settings, {})
    update = _make_update("/mcp")
    context = _make_context(settings)
    context.bot_data["claude_integration"] = _fake_integration()

    await orchestrator.agentic_mcp(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "telegram" in text
    assert "settings.json" in text
    assert "Claude Code (SDK)" in text


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_page_keyboard_disables_edges():
    """Page keyboard: no prev on first page, no next on last page."""
    from src.bot.handlers.capabilities import _page_keyboard

    kb = _page_keyboard("skills", 0, 3, "pdf")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "cap:page:skills:1:pdf" in data
    assert "cap:noop" in data  # disabled prev + page indicator
    assert not any("page:skills:-1" in d for d in data)

    kb_last = _page_keyboard("plugins", 2, 3)
    data_last = [b.callback_data for row in kb_last.inline_keyboard for b in row]
    assert "cap:page:plugins:1" in data_last
    assert not any("plugins:3" in d for d in data_last)

    assert _page_keyboard("skills", 0, 1) is None  # single page -> no buttons


async def test_page_callback_switches_page(tmp_path, fake_home):
    """cap:page:skills:<page> callback re-renders the requested page."""
    from src.bot.orchestrator import MessageOrchestrator

    cfg = tmp_path / "claude"
    for i in range(30):
        d = cfg / "skills" / f"skill-{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# Skill {i}\n", encoding="utf-8")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    orchestrator = MessageOrchestrator(settings, {})
    query = MagicMock()
    query.data = "cap:page:skills:1"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = _make_context(settings)

    await orchestrator._handle_page_callback(update, context)

    text = query.edit_message_text.call_args.args[0]
    assert "skill-12" in text
    assert "2/3" in text
    kwargs = query.edit_message_text.call_args.kwargs
    assert kwargs["reply_markup"] is not None


async def test_plugins_paginated(tmp_path):
    """/plugins paginates when many plugins are installed."""
    cfg = tmp_path / "claude"
    plugins_dir = cfg / "plugins"
    plugins_dir.mkdir(parents=True)
    names = {f"plugin-{i:02d}@market": [{"version": "1.0.0"}] for i in range(15)}
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"plugins": names}), encoding="utf-8"
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/plugins")
    context = _make_context(settings)

    await plugins_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Total: 15" in text
    assert "1/2" in text
    markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "cap:page:plugins:1" in data


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------


async def test_skills_command_lists_project_skills(tmp_path, fake_home):
    """/skills lists SKILL.md files under .claude/skills with descriptions."""
    project = tmp_path / "project"
    skills_root = project / ".claude" / "skills" / "reviewer"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        "# Reviewer\n\nCode review skill.\n", encoding="utf-8"
    )
    settings = create_test_config(approved_directory=str(project), agentic_mode=True)
    update = _make_update("/skills")
    context = _make_context(settings)

    await skills_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "reviewer" in text
    assert "Reviewer" in text  # description from first heading
    assert "Project" in text
    assert "Total:" in text


async def test_skills_command_lists_config_dir_skills(tmp_path, fake_home):
    """/skills shows the real Claude config dir skills under AI-Tools scope."""
    cfg = tmp_path / "claude"
    skill_dir = cfg / "skills" / "paperclip"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Paperclip\n\nAgent skills.\n", encoding="utf-8"
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/skills")
    context = _make_context(settings)

    await skills_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "AI-Tools" in text
    assert "paperclip" in text
    assert "Total: 1" in text


async def test_skills_command_truncates_long_output(tmp_path, fake_home):
    """/skills paginates huge skill sets instead of overflowing Telegram."""
    cfg = tmp_path / "claude"
    for i in range(50):
        d = cfg / "skills" / f"skill-{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# Skill {i}\n", encoding="utf-8")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/skills")
    context = _make_context(settings)

    await skills_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert len(text) < 4096
    assert "Total: 50" in text
    assert "1/5" in text  # page 1 of 5
    # navigation buttons attached
    markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
    assert markup is not None
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "cap:page:skills:1" in data


async def test_render_skills_pages_second_page(tmp_path, fake_home):
    """render_skills_pages returns distinct content per page + clamps bounds."""
    from src.bot.handlers.capabilities import render_skills_pages

    cfg = tmp_path / "claude"
    for i in range(30):
        d = cfg / "skills" / f"skill-{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# Skill {i}\n", encoding="utf-8")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )

    page0, total = render_skills_pages(settings, "", 0, None)
    page1, _ = render_skills_pages(settings, "", 1, None)
    last, _ = render_skills_pages(settings, "", 999, None)  # clamps to last page

    assert total == 3  # 30 skills / 12 per page
    assert "skill-00" in page0
    assert "skill-12" in page1
    assert "skill-29" in last
    assert "3/3" in last


async def test_skills_command_filters_by_keyword(tmp_path, fake_home):
    """/skills <keyword> only lists matching skills."""
    cfg = tmp_path / "claude"
    for name, title in (("review-bot", "Review"), ("writer", "Writer")):
        d = cfg / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {title}\n", encoding="utf-8")
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        claude_config_dir=str(cfg),
    )
    update = _make_update("/skills review")
    context = _make_context(settings)
    context.args = ["review"]

    await skills_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "review-bot" in text
    assert "writer" not in text
    assert "Total: 1" in text
    assert "Filter:" in text


# ---------------------------------------------------------------------------
# /features
# ---------------------------------------------------------------------------


async def test_features_command_shows_toggles(tmp_path):
    """/features reflects enabled/disabled feature flags."""
    mcp_cfg = tmp_path / "mcp.json"
    mcp_cfg.write_text(
        json.dumps({"mcpServers": {"demo": {"command": "npx", "args": ["demo"]}}}),
        encoding="utf-8",
    )
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        enable_scheduler=True,
        enable_api_server=True,
        enable_mcp=True,
        mcp_config_path=str(mcp_cfg),
    )
    update = _make_update("/features")
    context = _make_context(settings)

    await features_command(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "scheduler" in text
    assert "api_server" in text
    assert "mcp" in text
    assert "ENABLE_SCHEDULER=true" in text
