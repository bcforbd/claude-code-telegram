"""Tests for the remote approval bridge."""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.bot.features.approvals import (
    CALLBACK_PREFIX,
    ApprovalBridge,
    _summarize_input,
)


class _FakeMessage:
    """Minimal stand-in for a Telegram Message."""

    def __init__(self, message_id: int = 1):
        self.message_id = message_id
        self.edited: list = []

    async def edit_text(self, *args, **kwargs) -> None:
        self.edited.append((args, kwargs))


class _FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = _FakeMessage(message_id=len(self.sent) + 1)
        self.sent.append((chat_id, text, kwargs))
        return msg

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        pass


@pytest.fixture
def bridge():
    bot = _FakeBot()
    return ApprovalBridge(bot=bot, timeout_seconds=10), bot


class TestSummarizeInput:
    def test_empty(self) -> None:
        assert _summarize_input({}) == "(no input)"

    def test_json(self) -> None:
        text = _summarize_input({"command": "ls"})
        assert "ls" in text

    def test_truncation(self) -> None:
        big = {"data": "x" * 5000}
        text = _summarize_input(big)
        assert len(text) <= 1600
        assert text.endswith("…")


class TestApprovalBridge:
    async def test_allow_decision(self, bridge) -> None:
        bridge_obj, bot = bridge
        token = bridge_obj.push_chat(42)
        try:
            task = asyncio.create_task(
                bridge_obj.ask(
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    context=None,
                )
            )
            await asyncio.sleep(0)
            # find the pending request
            assert bridge_obj.pending_count() == 1
            request_id = next(iter(bridge_obj._pending))
            resolved, tool = bridge_obj.submit(request_id, "allow")
            assert resolved is True
            assert tool == "Bash"
            result = await task
            assert isinstance(result, PermissionResultAllow)
        finally:
            bridge_obj.pop_chat(token)
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == 42
        assert "Bash" in bot.sent[0][1]

    async def test_deny_decision(self, bridge) -> None:
        bridge_obj, _ = bridge
        token = bridge_obj.push_chat(7)
        try:
            task = asyncio.create_task(
                bridge_obj.ask(
                    tool_name="Write",
                    tool_input={"file_path": "/x"},
                    context=None,
                )
            )
            await asyncio.sleep(0)
            request_id = next(iter(bridge_obj._pending))
            bridge_obj.submit(request_id, "deny")
            result = await task
            assert isinstance(result, PermissionResultDeny)
        finally:
            bridge_obj.pop_chat(token)

    async def test_always_allow_caches_tool(self, bridge) -> None:
        bridge_obj, _ = bridge
        token = bridge_obj.push_chat(9)
        try:
            task = asyncio.create_task(
                bridge_obj.ask(
                    tool_name="WebFetch",
                    tool_input={"url": "https://x"},
                    context=None,
                )
            )
            await asyncio.sleep(0)
            request_id = next(iter(bridge_obj._pending))
            bridge_obj.submit(request_id, "always")
            result = await task
            assert isinstance(result, PermissionResultAllow)
            assert "WebFetch" in bridge_obj.always_allow(9)

            # Second call is auto-allowed without asking
            second = await bridge_obj.ask(
                tool_name="WebFetch",
                tool_input={"url": "https://y"},
                context=None,
            )
            assert isinstance(second, PermissionResultAllow)
        finally:
            bridge_obj.pop_chat(token)

    async def test_timeout_denies(self) -> None:
        bot = _FakeBot()
        bridge_obj = ApprovalBridge(bot=bot, timeout_seconds=0.05)
        token = bridge_obj.push_chat(3)
        try:
            result = await bridge_obj.ask(
                tool_name="Bash",
                tool_input={"command": "rm -rf /"},
                context=None,
            )
            assert isinstance(result, PermissionResultDeny)
            assert bridge_obj.pending_count() == 0
        finally:
            bridge_obj.pop_chat(token)

    async def test_no_chat_context_denies(self) -> None:
        bot = _FakeBot()
        bridge_obj = ApprovalBridge(bot=bot)
        result = await bridge_obj.ask(
            tool_name="Bash", tool_input={"command": "ls"}, context=None
        )
        assert isinstance(result, PermissionResultDeny)

    async def test_unknown_request_submit(self, bridge) -> None:
        bridge_obj, _ = bridge
        resolved, tool = bridge_obj.submit("does-not-exist", "allow")
        assert resolved is False
        assert tool == "unknown"

    def test_parse_callback_data(self) -> None:
        parsed = ApprovalBridge.parse_callback_data(f"{CALLBACK_PREFIX}abc123:allow")
        assert parsed == ("abc123", "allow")
        assert ApprovalBridge.parse_callback_data("other:x:y") is None
        assert ApprovalBridge.parse_callback_data(f"{CALLBACK_PREFIX}abc:maybe") is None

    async def test_send_message_injection(self) -> None:
        sent = []

        async def fake_send(chat_id, text, reply_markup=None):
            sent.append(text)
            return _FakeMessage()

        bridge_obj = ApprovalBridge(send_message=fake_send, timeout_seconds=10)
        token = bridge_obj.push_chat(11)
        try:
            task = asyncio.create_task(
                bridge_obj.ask(
                    tool_name="Edit",
                    tool_input={"file_path": "/a"},
                    context=None,
                )
            )
            await asyncio.sleep(0)
            request_id = next(iter(bridge_obj._pending))
            bridge_obj.submit(request_id, "allow")
            result = await task
            assert isinstance(result, PermissionResultAllow)
        finally:
            bridge_obj.pop_chat(token)
        assert len(sent) == 1
        assert "Edit" in sent[0]
