"""Remote approval bridge.

Routes Claude SDK tool permission requests to Telegram inline buttons
(Approve / Deny / Always allow) and waits for the user's decision.

The SDK ``can_use_tool`` callback runs inside the bot's event loop, so the
bridge can await a per-request ``asyncio.Future`` resolved by the inline
button callback handler.

Thread-safety across concurrent chats: the chat id is carried in a
``contextvars.ContextVar`` set by the orchestrator around each run, so
concurrent executions in different chats never cross wires.
"""

from __future__ import annotations

import asyncio
import html
import secrets
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import structlog
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

CALLBACK_PREFIX = "approve:"

_approval_chat_id: ContextVar[Optional[int]] = ContextVar(
    "approval_chat_id", default=None
)

_DECISION_LABELS = {
    "allow": "✅ Approved",
    "deny": "⛔ Denied",
    "always": "♻️ Always allow",
}

# How much of the tool input to show in the approval prompt.
_MAX_INPUT_CHARS = 1500


def _summarize_input(tool_input: Dict[str, Any]) -> str:
    """Compact human-readable tool input summary."""
    if not tool_input:
        return "(no input)"
    try:
        import json

        text = json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(tool_input)
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS] + "…"
    return text


class ApprovalBridge:
    """Sends permission prompts and resolves decisions."""

    def __init__(
        self,
        bot: Any = None,
        timeout_seconds: int = 120,
        send_message: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self.bot = bot
        self.timeout_seconds = timeout_seconds
        self._send_message = send_message
        # request_id -> (future, tool_name, chat_id, message)
        self._pending: Dict[str, Tuple[asyncio.Future, str, int, Any]] = {}
        # chat_id -> set of tool names always allowed for the session
        self._always_allow: Dict[int, set] = {}

    # ------------------------------------------------------------------
    # Chat context (set by the orchestrator around each run)
    # ------------------------------------------------------------------

    def push_chat(self, chat_id: int) -> Any:
        """Set the chat id for the current execution; returns a token."""
        return _approval_chat_id.set(chat_id)

    def pop_chat(self, token: Any) -> None:
        """Restore the previous chat context."""
        _approval_chat_id.reset(token)

    def current_chat_id(self) -> Optional[int]:
        """Chat id for the current execution context."""
        return _approval_chat_id.get()

    # ------------------------------------------------------------------
    # SDK callback
    # ------------------------------------------------------------------

    async def ask(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        context: Any = None,
    ) -> Any:
        """Ask the user for permission; returns PermissionResultAllow/Deny."""
        chat_id = self.current_chat_id()
        if chat_id is None:
            logger.warning(
                "Approval requested outside chat context; denying",
                tool_name=tool_name,
            )
            return PermissionResultDeny(message="No chat context")

        always = self._always_allow.get(chat_id, set())
        if tool_name in always:
            logger.info("Tool in always-allow set", tool_name=tool_name)
            return PermissionResultAllow()

        request_id = secrets.token_hex(8)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        summary = _summarize_input(tool_input)
        text = (
            f"🛡️ <b>{html.escape(tool_name)}</b> wants to run:\n"
            f"<code>{html.escape(summary)}</code>"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"{CALLBACK_PREFIX}{request_id}:allow",
                    ),
                    InlineKeyboardButton(
                        "⛔ Deny",
                        callback_data=f"{CALLBACK_PREFIX}{request_id}:deny",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "♻️ Always allow",
                        callback_data=f"{CALLBACK_PREFIX}{request_id}:always",
                    )
                ],
            ]
        )

        message = await self._send_prompt(chat_id=chat_id, text=text, keyboard=keyboard)
        self._pending[request_id] = (future, tool_name, chat_id, message)

        try:
            decision = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("Approval timed out; denying", tool_name=tool_name)
            await self._expire_prompt(chat_id, message, tool_name)
            return PermissionResultDeny(message="Approval timed out")
        finally:
            self._pending.pop(request_id, None)

        if decision == "always":
            self._always_allow.setdefault(chat_id, set()).add(tool_name)
            return PermissionResultAllow()
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user")

    async def _send_prompt(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup
    ) -> Any:
        if self._send_message is not None:
            return await self._send_message(
                chat_id=chat_id, text=text, reply_markup=keyboard
            )
        if self.bot is None:  # pragma: no cover - defensive
            raise RuntimeError("ApprovalBridge has no bot or send_message")
        return await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _expire_prompt(self, chat_id: int, message: Any, tool_name: str) -> None:
        """Mark the prompt as timed out (best-effort edit)."""
        try:
            if message is not None and hasattr(message, "edit_text"):
                await message.edit_text(
                    f"⏰ <b>{html.escape(tool_name)}</b> approval timed out "
                    "(denied).",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            elif self.bot is not None:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    text=f"⏰ <b>{html.escape(tool_name)}</b> approval timed "
                    "out (denied).",
                    parse_mode="HTML",
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to update expired prompt", error=str(exc))

    # ------------------------------------------------------------------
    # Callback handler side
    # ------------------------------------------------------------------

    def submit(self, request_id: str, decision: str) -> Tuple[bool, str]:
        """Resolve a pending request (called from the callback handler).

        Returns (resolved, tool_name).
        """
        pending = self._pending.get(request_id)
        if pending is None:
            logger.debug("Approval request not pending", request_id=request_id)
            return False, "unknown"
        future, tool_name, _chat_id, message = pending
        if future.done():
            return False, tool_name
        if decision not in _DECISION_LABELS:
            return False, tool_name
        future.set_result(decision)
        return True, tool_name

    def pending_count(self) -> int:
        """Number of approval requests awaiting a decision."""
        return len(self._pending)

    def always_allow_all(self) -> Dict[int, set]:
        """Copy of the per-chat always-allow sets (for diagnostics)."""
        return {chat: set(tools) for chat, tools in self._always_allow.items()}

    def always_allow(self, chat_id: int) -> set:
        """Tool names always allowed for a chat this session."""
        return set(self._always_allow.get(chat_id, set()))

    @staticmethod
    def parse_callback_data(data: str) -> Optional[Tuple[str, str]]:
        """Parse ``approve:<request_id>:<decision>`` -> (request_id, decision)."""
        if not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[2] not in _DECISION_LABELS:
            return None
        return parts[1], parts[2]
