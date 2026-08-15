"""Callback handler for remote approval buttons (approve:<request>:<decision>)."""

from __future__ import annotations

import html
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..features.approvals import (
    _DECISION_LABELS,
    ApprovalBridge,
)

logger = structlog.get_logger()


async def approval_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline approval button presses."""
    query = update.callback_query
    if query is None or not query.data:
        return

    parsed = ApprovalBridge.parse_callback_data(query.data)
    if parsed is None:
        return

    request_id, decision = parsed
    bridge: Optional[ApprovalBridge] = context.bot_data.get("approval_bridge")
    if bridge is None:
        await query.answer("Approval bridge not available")
        return

    resolved, tool_name = bridge.submit(request_id, decision)
    label = _DECISION_LABELS.get(decision, decision)
    if not resolved:
        await query.answer("This request is no longer pending")
        return

    await query.answer(f"{label}: {tool_name}")
    try:
        await query.edit_message_text(
            f"{label} <b>{html.escape(tool_name)}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to edit approval prompt", error=str(exc))
