"""Shared command handlers: stop, cost, stats, sessions, context, jobs, doctor.

These handlers are registered in BOTH agentic and classic modes (they read
everything they need from ``context.bot_data``), so there is a single
implementation instead of duplicated agentic/classic pairs.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ..utils.html_format import escape_html

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# /stop
# ---------------------------------------------------------------------------


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interrupt the user's active Claude request: /stop."""
    user_id = update.effective_user.id
    orchestrator = context.bot_data.get("orchestrator")
    if orchestrator is None:
        await update.message.reply_text(
            "❌ <b>Stop Unavailable</b>\n\n" "The request manager is not initialized.",
            parse_mode="HTML",
        )
        return

    stopped = await orchestrator.interrupt_user(user_id)
    if stopped:
        await update.message.reply_text(
            "⏹️ <b>Stopping Claude…</b>\n\n"
            "The current request is being interrupted. The session stays "
            "active so you can continue afterwards.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No active request to stop.",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# /cost
# ---------------------------------------------------------------------------


async def cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Real spend + budget for this user: /cost."""
    user_id = update.effective_user.id
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    if integration is None:
        await update.message.reply_text("❌ Claude integration not available.")
        return

    sessions: List[Dict[str, Any]] = []
    try:
        sessions = await integration.get_user_sessions(user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_sessions failed in /cost", error=str(exc))

    total_cost = sum(float(s.get("total_cost", 0.0)) for s in sessions)
    current_session_id = context.user_data.get("claude_session_id")
    current_cost = 0.0
    if current_session_id:
        for s in sessions:
            if s.get("session_id") == current_session_id:
                current_cost = float(s.get("total_cost", 0.0))
                break

    # Budget from the rate limiter (in-memory, per user)
    budget_line = ""
    rate_limiter = context.bot_data.get("rate_limiter")
    if rate_limiter is not None:
        try:
            status = rate_limiter.get_user_status(user_id)
            cost_usage = status.get("cost_usage", {})
            limit = float(cost_usage.get("limit", 0.0))
            remaining = float(cost_usage.get("remaining", 0.0))
            if limit > 0:
                pct = cost_usage.get("utilization", 0.0) * 100
                budget_line = (
                    f"\n💰 <b>Budget:</b> ${remaining:.2f} remaining "
                    f"of ${limit:.2f} ({pct:.1f}% used)"
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Rate limiter status failed in /cost", error=str(exc))

    backend = integration.get_active_backend_name(context.user_data.get("backend"))
    backend_display = integration.backend_display(backend)
    model_override = context.user_data.get("model_override")

    lines = [
        "💵 <b>Usage & Cost</b>",
        "",
        f"🔌 Backend: <b>{escape_html(backend_display)}</b>",
    ]
    if model_override:
        lines.append(f"🧠 Model override: <code>{escape_html(model_override)}</code>")
    lines += [
        "",
        f"🧾 Current session: <b>${current_cost:.4f}</b>",
        f"📚 All sessions: <b>${total_cost:.4f}</b>",
        f"🔁 Session count: <b>{len(sessions)}</b>",
    ]
    if budget_line:
        lines.append(budget_line)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage dashboard: /stats."""
    user_id = update.effective_user.id
    storage = context.bot_data.get("storage")
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    if storage is None or integration is None:
        await update.message.reply_text("❌ Stats unavailable (storage not ready).")
        return

    dashboard = None
    try:
        dashboard = await storage.get_user_dashboard(user_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_dashboard failed in /stats", error=str(exc))

    sessions: List[Dict[str, Any]] = []
    try:
        sessions = await integration.get_user_sessions(user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_sessions failed in /stats", error=str(exc))

    total_cost = sum(float(s.get("total_cost", 0.0)) for s in sessions)
    total_msgs = sum(int(s.get("message_count", 0)) for s in sessions)

    lines = [
        "📊 <b>Your Stats</b>",
        "",
        f"🔁 Sessions: <b>{len(sessions)}</b>",
        f"💬 Messages: <b>{total_msgs}</b>",
        f"💵 Total cost: <b>${total_cost:.4f}</b>",
    ]

    if dashboard:
        user = dashboard.get("user") or {}
        if user.get("last_active"):
            lines.append(
                f"🕒 Last active: <code>{escape_html(str(user['last_active']))}</code>"
            )
        daily = dashboard.get("daily_costs") or []
        if daily:
            lines.append("")
            lines.append("<b>Last 7 days:</b>")
            recent = daily[-7:]
            for day in recent:
                date = str(day.get("date") or day.get("day") or "?")
                cost = float(day.get("total_cost", day.get("cost", 0.0)) or 0.0)
                lines.append(f"  • <code>{escape_html(date)}</code> — ${cost:.4f}")

    rate_limiter = context.bot_data.get("rate_limiter")
    if rate_limiter is not None:
        try:
            status = rate_limiter.get_user_status(user_id)
            bucket = status.get("request_bucket", {})
            tokens = bucket.get("tokens", bucket.get("current", "?"))
            capacity = bucket.get("capacity", "?")
            lines.append("")
            lines.append(
                f"⚡ Rate limit: <b>{tokens}/{capacity}</b> tokens "
                f"(requests per {status.get('window_seconds', 60)}s window)"
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Rate limiter status failed in /stats", error=str(exc))

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /sessions + sess: callback
# ---------------------------------------------------------------------------

_CALLBACK_PREFIX = "sess:"


async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List sessions with inline switch buttons: /sessions."""
    user_id = update.effective_user.id
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    if integration is None:
        await update.message.reply_text("❌ Claude integration not available.")
        return

    try:
        sessions = await integration.get_user_sessions(user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_sessions failed in /sessions", error=str(exc))
        sessions = []

    if not sessions:
        await update.message.reply_text(
            "📂 <b>No sessions yet.</b>\n\n"
            "Send a message to start your first conversation.",
            parse_mode="HTML",
        )
        return

    current_session_id = context.user_data.get("claude_session_id")
    # Most recently used first
    sessions = sorted(sessions, key=lambda s: str(s.get("last_used", "")), reverse=True)
    shown = sessions[:8]

    keyboard = []
    for s in shown:
        sid = s.get("session_id", "")
        project = Path(str(s.get("project_path", "?"))).name or "?"
        msgs = int(s.get("message_count", 0))
        cost = float(s.get("total_cost", 0.0))
        marker = "🟢" if sid == current_session_id else "📁"
        label = f"{marker} {project} · {msgs} msgs · ${cost:.2f}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"{_CALLBACK_PREFIX}{sid}")]
        )

    lines = [
        "📂 <b>Your Sessions</b>",
        "",
        "Tap a session to switch to it. 🟢 = active.",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_sessions_callback(
    query, session_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Switch the user's active session: sess:<session_id>."""
    user_id = query.from_user.id
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    if integration is None:
        await query.answer("Claude integration not available", show_alert=True)
        return

    try:
        sessions = await integration.get_user_sessions(user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_sessions failed in sess: callback", error=str(exc))
        sessions = []

    target = next((s for s in sessions if s.get("session_id") == session_id), None)
    if target is None:
        await query.answer("Session not found", show_alert=True)
        return

    # Switch context: resume this session in its project directory.
    context.user_data["claude_session_id"] = session_id
    context.user_data["session_started"] = True
    project_path = target.get("project_path")
    if project_path:
        context.user_data["current_directory"] = Path(project_path)

    project = Path(str(project_path or "?")).name or "?"
    await query.answer(f"Switched to {project}")
    try:
        await query.edit_message_text(
            f"🟢 <b>Switched session</b>\n\n"
            f"📁 Project: <code>{escape_html(project)}</code>\n"
            f"💬 Messages: {int(target.get('message_count', 0))}\n"
            f"💵 Cost: ${float(target.get('total_cost', 0.0)):.4f}\n\n"
            "Next message continues this session.",
            parse_mode="HTML",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to edit sessions message", error=str(exc))


# ---------------------------------------------------------------------------
# /context
# ---------------------------------------------------------------------------


async def context_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect the current session: /context."""
    user_id = update.effective_user.id
    session_id = context.user_data.get("claude_session_id")
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    if integration is None:
        await update.message.reply_text("❌ Claude integration not available.")
        return

    if not session_id:
        await update.message.reply_text(
            "ℹ️ <b>No active session.</b>\n\n"
            "Send a message to start one, or use /sessions to resume.",
            parse_mode="HTML",
        )
        return

    session_info: Optional[Dict[str, Any]] = None
    try:
        sessions = await integration.get_user_sessions(user_id) or []
        session_info = next(
            (s for s in sessions if s.get("session_id") == session_id), None
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_user_sessions failed in /context", error=str(exc))

    # Message count + tools from storage history
    storage = context.bot_data.get("storage")
    history_msgs: Optional[int] = None
    tools: List[str] = []
    if storage is not None:
        try:
            history = await storage.get_session_history(session_id, limit=500)
            if history:
                history_msgs = len(history.get("messages", []) or [])
                tools = [
                    str(t.get("tool_name", ""))
                    for t in (history.get("tool_usage", []) or [])
                ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("get_session_history failed in /context", error=str(exc))

    lines = ["🧠 <b>Session Context</b>", ""]
    if session_info is None:
        lines.append(f"🆔 <code>{escape_html(session_id[:16])}…</code>")
    else:
        project = Path(str(session_info.get("project_path", "?"))).name or "?"
        lines.append(f"📁 Project: <b>{escape_html(project)}</b>")
        lines.append(f"🆔 <code>{escape_html(session_id)}</code>")
        lines.append(
            f"🗓 Created: <code>{escape_html(str(session_info.get('created_at', '?')))}</code>"
        )
        lines.append(
            f"🕒 Last used: <code>{escape_html(str(session_info.get('last_used', '?')))}</code>"
        )
        lines.append(
            f"💬 Stored messages: <b>{int(session_info.get('message_count', 0))}</b>"
        )
        lines.append(
            f"💵 Cost: <b>${float(session_info.get('total_cost', 0.0)):.4f}</b>"
        )
    if history_msgs is not None:
        lines.append(f"🗂 History rows: <b>{history_msgs}</b>")
    if tools:
        unique = sorted(set(tools))[:10]
        lines.append(f"🛠 Tools used: <code>{escape_html(', '.join(unique))}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage scheduled jobs: /jobs | /jobs add <name> <cron> <prompt> | /jobs remove <id>."""
    scheduler = context.bot_data.get("scheduler")
    if scheduler is None:
        await update.message.reply_text(
            "ℹ️ <b>Scheduler disabled.</b>\n\n"
            "Set <code>ENABLE_SCHEDULER=true</code> in the bot config to use "
            "scheduled jobs.",
            parse_mode="HTML",
        )
        return

    text = (update.message.text or "").strip()
    try:
        import shlex

        args = shlex.split(text)[1:]
    except ValueError as exc:
        await update.message.reply_text(
            f"❌ Could not parse arguments: {escape_html(str(exc))}\n\n"
            "Quote the cron expression, e.g.\n"
            '<code>/jobs add myjob "0 9 * * *" run tests</code>',
            parse_mode="HTML",
        )
        return
    if not args:
        await _jobs_list(update, context, scheduler)
        return

    sub = args[0].lower()
    if sub == "add" and len(args) >= 4:
        await _jobs_add(update, context, scheduler, args[1:])
    elif sub == "remove" and len(args) == 2:
        await _jobs_remove(update, context, scheduler, args[1])
    else:
        await update.message.reply_text(
            "📅 <b>Usage:</b>\n"
            "<code>/jobs</code> — list jobs\n"
            '<code>/jobs add &lt;name&gt; &lt;"cron"&gt; &lt;prompt...&gt;</code> — create\n'
            "<code>/jobs remove &lt;job_id&gt;</code> — delete\n\n"
            "Cron format: <code>min hour day month weekday</code> "
            '(e.g. <code>"0 9 * * 1-5"</code> = weekdays 09:00).',
            parse_mode="HTML",
        )


async def _jobs_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, scheduler
) -> None:
    """Render the job list."""
    try:
        jobs = await scheduler.list_jobs()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("list_jobs failed in /jobs", error=str(exc))
        await update.message.reply_text(
            f"❌ Failed to list jobs: {escape_html(str(exc))}"
        )
        return

    if not jobs:
        await update.message.reply_text(
            "📅 <b>No scheduled jobs.</b>\n\n"
            "Add one with:\n"
            "<code>/jobs add myjob '0 9 * * *' 'run tests and report'</code>",
            parse_mode="HTML",
        )
        return

    lines = ["📅 <b>Scheduled Jobs</b>", ""]
    for job in jobs:
        jid = str(job.get("job_id", "?"))
        name = str(job.get("job_name", "?"))
        cron = str(job.get("cron_expression", "?"))
        prompt = str(job.get("prompt", ""))[:60]
        lines.append(
            f"• <b>{escape_html(name)}</b> <code>{escape_html(jid[:8])}</code>\n"
            f"  ⏰ {escape_html(cron)}\n"
            f"  💬 {escape_html(prompt)}"
        )
    lines.append("")
    lines.append("Remove: <code>/jobs remove &lt;job_id&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _jobs_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scheduler,
    args: List[str],
) -> None:
    """Create a scheduled job."""
    name = args[0]
    cron = args[1]
    prompt = " ".join(args[2:])
    if not prompt:
        await update.message.reply_text(
            "❌ Missing prompt. Usage:\n"
            "<code>/jobs add &lt;name&gt; &lt;cron&gt; &lt;prompt...&gt;</code>"
        )
        return

    # Validate cron expression
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Invalid cron expression <code>{escape_html(cron)}</code>: "
            f"{escape_html(str(exc))}",
            parse_mode="HTML",
        )
        return

    settings = context.bot_data.get("settings")
    working_dir = context.user_data.get(
        "current_directory",
        settings.approved_directory if settings else None,
    )
    chat_id = update.effective_chat.id if update.effective_chat else None

    try:
        job_id = await scheduler.add_job(
            job_name=name,
            cron_expression=cron,
            prompt=prompt,
            target_chat_ids=[chat_id] if chat_id else None,
            working_directory=working_dir,
            created_by=update.effective_user.id,
        )
    except Exception as exc:
        logger.warning("add_job failed in /jobs add", error=str(exc))
        await update.message.reply_text(
            f"❌ Failed to create job: {escape_html(str(exc))}",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"✅ <b>Job created</b>\n\n"
        f"🆔 <code>{escape_html(str(job_id))}</code>\n"
        f"📛 Name: <b>{escape_html(name)}</b>\n"
        f"⏰ Cron: <code>{escape_html(cron)}</code>\n"
        f"💬 Prompt: {escape_html(prompt[:120])}",
        parse_mode="HTML",
    )


async def _jobs_remove(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scheduler,
    job_id: str,
) -> None:
    """Remove a scheduled job."""
    try:
        removed = await scheduler.remove_job(job_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("remove_job failed in /jobs remove", error=str(exc))
        await update.message.reply_text(
            f"❌ Failed to remove job: {escape_html(str(exc))}",
            parse_mode="HTML",
        )
        return
    if removed:
        await update.message.reply_text(
            f"🗑 Removed job <code>{escape_html(job_id)}</code>."
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Job <code>{escape_html(job_id)}</code> not found or already removed."
        )


# ---------------------------------------------------------------------------
# /doctor
# ---------------------------------------------------------------------------


async def doctor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run diagnostics and render a compact checklist: /doctor."""
    user_id = update.effective_user.id
    checks: List[tuple] = []  # (label, ok: bool|None, detail)
    settings = context.bot_data.get("settings")
    integration: Optional[ClaudeIntegration] = context.bot_data.get(
        "claude_integration"
    )
    storage = context.bot_data.get("storage")

    # Auth
    allowed = settings.allowed_users if settings else None
    checks.append(
        (
            "Authorization",
            (allowed is None or user_id in allowed),
            (
                "whitelisted"
                if (allowed is None or user_id in allowed)
                else "NOT whitelisted"
            ),
        )
    )

    # SDK + CLI
    try:
        import claude_agent_sdk

        sdk_version = getattr(claude_agent_sdk, "__version__", "?")
        checks.append(("Claude Agent SDK", True, f"v{sdk_version}"))
    except Exception as exc:
        checks.append(("Claude Agent SDK", False, str(exc)))

    cli_path = shutil.which("claude")
    checks.append(("Claude CLI on PATH", cli_path is not None, cli_path or "not found"))

    api_key = None
    if settings is not None:
        api_key = settings.anthropic_api_key
    checks.append(
        (
            "API key configured",
            bool(api_key),
            "yes" if api_key else "using CLI auth / none",
        )
    )

    # Backends
    if integration is not None:
        try:
            status = await integration.get_backend_status()
            active = status.get("active_default", "?")
            checks.append(
                (
                    "Backends",
                    bool(status.get("available")),
                    f"active={active} available={status.get('available')}",
                )
            )
            for name, bstatus in (status.get("backends") or {}).items():
                ok = True
                detail = str(bstatus) if not isinstance(bstatus, dict) else ""
                if isinstance(bstatus, dict):
                    error = bstatus.get("error")
                    ok = error is None
                    detail = f"{bstatus.get('description', '')}".strip()
                    if bstatus.get("mcp_servers"):
                        detail += f" · MCP: {bstatus['mcp_servers']}"
                    if error:
                        detail += f" · error: {error}"
                checks.append((f"  backend:{name}", ok, detail or "ok"))
        except Exception as exc:
            checks.append(("Backends", False, str(exc)))

    # Storage
    if storage is not None:
        try:
            healthy = await storage.health_check()
            checks.append(("Storage", bool(healthy), "ok" if healthy else "unhealthy"))
        except Exception as exc:
            checks.append(("Storage", False, str(exc)))

    # Approval bridge
    bridge = context.bot_data.get("approval_bridge")
    if bridge is not None:
        pending = bridge.pending_count()
        always = sum(len(v) for v in bridge.always_allow_all().values())
        checks.append(
            ("Remote approvals", True, f"{pending} pending · {always} always-allow")
        )

    lines = ["🩺 <b>Doctor Report</b>", ""]
    for label, ok, detail in checks:
        icon = "✅" if ok else ("⚠️" if ok is None else "❌")
        lines.append(f"{icon} <b>{escape_html(label)}</b> — {escape_html(str(detail))}")
    lines.append("")
    lines.append("_Run /doctor after any config change._")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
