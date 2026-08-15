"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import capabilities, command, insights, manage

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("help", command.help_command),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("git", command.git_command),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("plugins", manage.plugins_manage),
            ("skills", manage.skills_manage),
            ("features", capabilities.features_command),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("restart", command.restart_command),
            ("backend", self.agentic_backend),
            ("desktop", self.agentic_desktop),
            ("cowork", self.agentic_cowork),
            ("model", self.agentic_model),
            ("permission", self.agentic_permission),
            ("mcp", manage.mcp_manage),
            ("stop", insights.stop_command),
            ("cost", insights.cost_command),
            ("stats", insights.stats_command),
            ("sessions", insights.sessions_command),
            ("context", insights.context_command),
            ("jobs", insights.jobs_command),
            ("doctor", insights.doctor_command),
            ("goal", manage.goal_command),
            ("loop", manage.loop_command),
            ("compact", manage.compact_command),
            ("clear", manage.clear_command),
            ("review", manage.review_command),
            ("todo", manage.todo_command),
            ("memory", manage.memory_command),
            ("hooks", manage.hooks_command),
            ("config", manage.config_command),
            ("plan", manage.plan_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Remote approval buttons (approve:<request>:<decision>)
        from .handlers.approval import approval_callback_handler

        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(approval_callback_handler),
                pattern=r"^approve:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # Backend selection buttons (backend:set:<name>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_backend_callback),
                pattern=r"^backend:set:",
            )
        )

        # Pagination buttons (cap:page:<kind>:<page>[:keyword])
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_page_callback),
                pattern=r"^cap:page:",
            )
        )
        # Page indicator / disabled buttons (cap:noop)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_noop_callback),
                pattern=r"^cap:noop$",
            )
        )

        # Cancel buttons (cancel:<context>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_cancel_callback),
                pattern=r"^cancel:",
            )
        )
        # Management confirmations (mng:confirm:<op>:<payload>)
        from .handlers import manage as manage_handlers

        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage_handlers.mcp_confirm_remove),
                pattern=r"^mng:confirm:mcp_remove:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage_handlers.mng_confirm),
                pattern=r"^mng:confirm:",
            )
        )
        # MCP management buttons (mng:mcp:<action>:<name>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage_handlers.mcp_button_callback),
                pattern=r"^mng:mcp:",
            )
        )

        # Generic callback router (quick actions, cd:, action:, confirm:,
        # followup:, conversation:, git:, export:, sess:) — same as classic.
        from .handlers import callback

        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, capabilities, command, insights, manage, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("plugins", capabilities.plugins_command),
            ("skills", capabilities.skills_command),
            ("features", capabilities.features_command),
            ("restart", command.restart_command),
            ("backend", command.backend_command),
            ("desktop", command.desktop_command),
            ("cowork", command.cowork_command),
            ("model", command.model_command),
            ("permission", command.permission_command),
            ("mcp", manage.mcp_manage),
            ("stop", insights.stop_command),
            ("cost", insights.cost_command),
            ("stats", insights.stats_command),
            ("sessions", insights.sessions_command),
            ("context", insights.context_command),
            ("jobs", insights.jobs_command),
            ("doctor", insights.doctor_command),
            ("goal", manage.goal_command),
            ("loop", manage.loop_command),
            ("compact", manage.compact_command),
            ("clear", manage.clear_command),
            ("review", manage.review_command),
            ("todo", manage.todo_command),
            ("memory", manage.memory_command),
            ("hooks", manage.hooks_command),
            ("config", manage.config_command),
            ("plan", manage.plan_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )

        # Remote approval buttons first, so the catch-all never sees them.
        from .handlers.approval import approval_callback_handler

        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(approval_callback_handler),
                pattern=r"^approve:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_backend_callback),
                pattern=r"^backend:set:",
            )
        )
        # Pagination buttons (cap:page:<kind>:<page>[:keyword])
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_page_callback),
                pattern=r"^cap:page:",
            )
        )
        # Page indicator / disabled buttons (cap:noop)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_noop_callback),
                pattern=r"^cap:noop$",
            )
        )
        # Cancel buttons (cancel:<context>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_cancel_callback),
                pattern=r"^cancel:",
            )
        )
        # MCP management buttons (mng:mcp:<action>:<name>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage.mcp_button_callback),
                pattern=r"^mng:mcp:",
            )
        )
        # MCP remove confirmation (specific before generic mng:confirm)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage.mcp_confirm_remove),
                pattern=r"^mng:confirm:mcp_remove:",
            )
        )
        # Management confirmations (mng:confirm:<op>:<payload>)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(manage.mng_confirm),
                pattern=r"^mng:confirm:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("help", "Show available commands"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("git", "Git repository commands"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("plugins", "List configured Claude plugins"),
                BotCommand("skills", "List available Claude skills"),
                BotCommand("features", "Show enabled feature toggles"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("backend", "Show/switch backend (sdk/desktop/cowork)"),
                BotCommand("desktop", "Claude Desktop config status"),
                BotCommand("cowork", "Claude Cowork profile status"),
                BotCommand("model", "Show/set model override"),
                BotCommand("permission", "Show/set permission mode"),
                BotCommand("mcp", "List active MCP servers"),
                BotCommand("stop", "Interrupt the running request"),
                BotCommand("cost", "Show real spend and budget"),
                BotCommand("stats", "Show usage dashboard"),
                BotCommand("sessions", "List / switch sessions"),
                BotCommand("context", "Inspect current session"),
                BotCommand("jobs", "Manage scheduled jobs"),
                BotCommand("doctor", "Run diagnostics"),
                BotCommand("goal", "Set a session goal"),
                BotCommand("loop", "Auto-continue the session"),
                BotCommand("compact", "Summarize + restart fresh"),
                BotCommand("clear", "Clear session context"),
                BotCommand("review", "Review current git diff"),
                BotCommand("todo", "Session todo list"),
                BotCommand("memory", "Show Claude memory (CLAUDE.md)"),
                BotCommand("hooks", "List configured hooks"),
                BotCommand("config", "Show effective config"),
                BotCommand("plan", "Toggle plan permission mode"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("plugins", "List/manage Claude plugins"),
                BotCommand("skills", "List/manage Claude skills"),
                BotCommand("features", "Show enabled feature toggles"),
                BotCommand("git", "Git repository commands"),
                BotCommand("backend", "Show/switch backend (sdk/desktop/cowork)"),
                BotCommand("desktop", "Claude Desktop config status"),
                BotCommand("cowork", "Claude Cowork profile status"),
                BotCommand("model", "Show/set model override"),
                BotCommand("permission", "Show/set permission mode"),
                BotCommand("mcp", "List/manage MCP servers"),
                BotCommand("stop", "Interrupt the running request"),
                BotCommand("cost", "Show real spend and budget"),
                BotCommand("stats", "Show usage dashboard"),
                BotCommand("sessions", "List / switch sessions"),
                BotCommand("context", "Inspect current session"),
                BotCommand("jobs", "Manage scheduled jobs"),
                BotCommand("doctor", "Run diagnostics"),
                BotCommand("goal", "Set a session goal"),
                BotCommand("loop", "Auto-continue the session"),
                BotCommand("compact", "Summarize + restart fresh"),
                BotCommand("clear", "Clear session context"),
                BotCommand("review", "Review current git diff"),
                BotCommand("todo", "Session todo list"),
                BotCommand("memory", "Show Claude memory (CLAUDE.md)"),
                BotCommand("hooks", "List configured hooks"),
                BotCommand("config", "Show effective config"),
                BotCommand("plan", "Toggle plan permission mode"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # Backend / desktop / cowork / model / permission / mcp commands
    # ------------------------------------------------------------------

    def _backend_run_params(self, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """Per-chat backend/model/permission overrides from user_data."""
        return {
            "backend": context.user_data.get("backend"),
            "model": context.user_data.get("model_override"),
            "permission_mode": context.user_data.get("permission_mode"),
        }

    def _enter_approval_chat(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int
    ) -> Any:
        """Bind the approval bridge to a chat for the current execution.

        Returns a contextvar token (or None when approvals are disabled) to
        pass to :meth:`_exit_approval_chat`.
        """
        bridge = context.bot_data.get("approval_bridge")
        if bridge is None:
            return None
        return bridge.push_chat(chat_id)

    def _exit_approval_chat(
        self, context: ContextTypes.DEFAULT_TYPE, token: Any
    ) -> None:
        """Leave the approval chat context bound by _enter_approval_chat."""
        if token is None:
            return
        bridge = context.bot_data.get("approval_bridge")
        if bridge is not None:
            bridge.pop_chat(token)

    def _require_integration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Return the ClaudeIntegration facade or None."""
        return context.bot_data.get("claude_integration")

    async def agentic_backend(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show/set backend: /backend [sdk|desktop|cowork]."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await update.message.reply_text("Claude integration not available.")
            return

        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = context.user_data.get("backend") or (
                claude_integration.router.select().name
            )
            keyboard = [
                [
                    InlineKeyboardButton(
                        ("✅ " if name == current else "")
                        + claude_integration.backend_display(name),
                        callback_data=f"backend:set:{name}",
                    )
                ]
                for name in claude_integration.router.available_backends()
            ]
            keyboard.append(
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel:backend")]
            )
            await update.message.reply_text(
                f"🧠 Backend: <b>{claude_integration.backend_display(current)}</b>\n\n"
                "Tap a button to switch (session resets):",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        name = args[0].strip().lower()
        if name not in claude_integration.router.available_backends():
            await update.message.reply_text(
                f"❌ Unknown backend <code>{name}</code>. "
                f"Available: {', '.join(claude_integration.router.available_backends())}",
                parse_mode="HTML",
            )
            return

        context.user_data["backend"] = name
        # Backend switch implies a fresh Claude session
        context.user_data["claude_session_id"] = None
        context.user_data["force_new_session"] = True
        await update.message.reply_text(
            f"🧠 Backend switched to <b>{claude_integration.backend_display(name)}</b>.\n"
            "Session reset — your next message starts fresh.",
            parse_mode="HTML",
        )

    async def _backend_status_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        backend_name: str,
        header: str,
    ) -> None:
        """Format a backend status snapshot as a compact reply."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await update.message.reply_text("Claude integration not available.")
            return
        statuses = await claude_integration.get_backend_status()
        status = statuses.get("backends", {}).get(backend_name, {})
        active = statuses.get("active_default")
        active_here = context.user_data.get("backend") or active

        lines = [header]
        if not status:
            lines.append("Backend not registered.")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        ok = status.get("ok")
        lines.append("Status: " + ("✅ configured" if ok else "⚠️ not configured"))
        lines.append(
            "Active here: "
            + ("✅" if active_here == backend_name else "—")
            + f" <code>{backend_name}</code>"
        )

        if backend_name == "desktop":
            path = status.get("config_path")
            lines.append(f"Config: <code>{path}</code>" if path else "Config: —")
            servers = status.get("mcp_servers") or []
            lines.append(
                "MCP servers: "
                + (", ".join(f"<code>{s}</code>" for s in servers) or "—")
            )
            gateway = status.get("gateway_base_url")
            lines.append(
                "Gateway: " + (f"<code>{gateway}</code>" if gateway else "default")
            )
            lines.append(
                f"Dev-mode env: {'✅' if status.get('developer_mode_env') else '—'}"
            )
        elif backend_name == "cowork":
            lines.append(f"Model: <code>{status.get('model')}</code>")
            lines.append(f"Permission: <code>{status.get('permission_mode')}</code>")
            lines.append(f"Web search: {'✅' if status.get('web_search') else '—'}")
            plugins = status.get("plugin_dirs") or []
            lines.append(
                "Plugins: " + (", ".join(f"<code>{p}</code>" for p in plugins) or "—")
            )
            prefs = status.get("preferences_loaded")
            lines.append(f"Preferences loaded: {'✅' if prefs else '—'}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def agentic_desktop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Claude Desktop config status: /desktop."""
        await self._backend_status_reply(
            update,
            context,
            "desktop",
            "🖥️ <b>Claude Desktop</b> (developer-mode mirror)",
        )

    async def agentic_cowork(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Claude Cowork profile status: /cowork."""
        await self._backend_status_reply(
            update, context, "cowork", "🤝 <b>Claude Cowork</b> profile"
        )

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show/set per-chat model override: /model [name|reset]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = context.user_data.get("model_override")
            await update.message.reply_text(
                "🧠 Model override: "
                + (f"<code>{current}</code>" if current else "none (default)")
                + "\n\nUsage: <code>/model claude-sonnet-4-5</code> · "
                "<code>/model reset</code> to clear",
                parse_mode="HTML",
            )
            return
        if args[0].strip().lower() in ("reset", "none", "-", "default"):
            context.user_data.pop("model_override", None)
            await update.message.reply_text(
                "Model override cleared — using the default model."
            )
            return
        context.user_data["model_override"] = args[0].strip()
        await update.message.reply_text(
            f"Model override set to <b>{args[0].strip()}</b>", parse_mode="HTML"
        )

    async def agentic_permission(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show/set permission mode: /permission [mode|reset]."""
        valid = {"acceptEdits", "plan", "bypassPermissions", "dontAsk", "manual"}
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = context.user_data.get("permission_mode")
            await update.message.reply_text(
                "🔐 Permission mode: "
                + (f"<code>{current}</code>" if current else "default")
                + "\n\nModes: "
                + ", ".join(f"<code>{m}</code>" for m in sorted(valid))
                + "\n<code>/permission reset</code> to clear",
                parse_mode="HTML",
            )
            return
        value = args[0].strip().lower()
        if value in ("reset", "none", "-", "default"):
            context.user_data.pop("permission_mode", None)
            await update.message.reply_text(
                "Permission mode cleared — using the backend default."
            )
            return
        canonical = {mode.lower(): mode for mode in valid}
        mode = canonical.get(value)
        if mode is None:
            await update.message.reply_text(
                f"❌ Unknown mode <code>{args[0].strip()}</code>. Valid: "
                + ", ".join(sorted(valid)),
                parse_mode="HTML",
            )
            return
        context.user_data["permission_mode"] = mode
        await update.message.reply_text(
            f"Permission mode set to <b>{mode}</b>", parse_mode="HTML"
        )

    async def agentic_mcp(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List active MCP servers for the current backend: /mcp."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await update.message.reply_text("Claude integration not available.")
            return
        active_name = context.user_data.get("backend") or (
            claude_integration.router.select().name
        )
        backend = claude_integration.backends.get(active_name)
        servers: List[str] = []
        try:
            if backend is not None and hasattr(backend, "mcp_servers"):
                servers = sorted(backend.mcp_servers().keys())
            elif self.settings.enable_mcp and self.settings.mcp_config_path:
                import json as _json

                try:
                    with open(self.settings.mcp_config_path) as f:
                        servers = sorted((_json.load(f).get("mcpServers") or {}).keys())
                except (OSError, ValueError):
                    servers = []
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("MCP listing failed", error=str(exc))
            servers = []

        lines = [
            f"🔌 MCP servers for <b>{claude_integration.backend_display(active_name)}</b>:"
        ]
        if servers:
            lines.append("\n".join(f"• <code>{s}</code>" for s in servers))
        else:
            lines.append("None configured.")
        from .handlers.capabilities import collect_mcp_servers

        extra = collect_mcp_servers(self.settings)
        if extra:
            for label, extra_servers in extra:
                lines.append(f"\n🧩 <b>{escape_html(label)}</b>:")
                lines.append("\n".join(f"• <code>{s}</code>" for s in extra_servers))
        else:
            lines.append(
                "\n💡 No MCP servers found. Set <code>CLAUDE_CONFIG_DIR</code> "
                "or <code>ENABLE_MCP=true</code> + <code>MCP_CONFIG_PATH</code>."
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        mcp_deliveries: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        When *mcp_deliveries* is provided, ``send_document_to_user`` and
        ``notify_user`` tool calls are collected for later delivery.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = (
            mcp_images is not None and approved_directory is not None
        ) or mcp_deliveries is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    tc_input = tc.get("input", {})
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)
                    elif mcp_deliveries is not None:
                        if tc_name == "send_document_to_user" or tc_name.endswith(
                            "__send_document_to_user"
                        ):
                            mcp_deliveries.append(
                                {
                                    "kind": "document",
                                    "file_path": tc_input.get("file_path", ""),
                                    "caption": tc_input.get("caption", ""),
                                }
                            )
                        elif tc_name == "notify_user" or tc_name.endswith(
                            "__notify_user"
                        ):
                            mcp_deliveries.append(
                                {
                                    "kind": "notify",
                                    "message": tc_input.get("message", ""),
                                }
                            )

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _deliver_mcp_messages(
        self,
        update: Update,
        deliveries: List[Dict[str, Any]],
    ) -> None:
        """Deliver MCP-requested documents and notifications to the user."""
        for item in deliveries:
            kind = item.get("kind")
            try:
                if kind == "document":
                    path = Path(item.get("file_path", ""))
                    if path.is_file():
                        await update.message.reply_document(
                            document=str(path),
                            filename=path.name,
                            caption=item.get("caption") or None,
                        )
                    else:
                        logger.warning("MCP document missing", path=str(path))
                elif kind == "notify":
                    await update.message.reply_text(item.get("message", ""))
            except Exception as exc:
                logger.warning("MCP delivery failed", kind=kind, error=str(exc))

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        # Route MCP OAuth redirect URLs to a pending login (paste-back flow)
        if message_text and (
            message_text.startswith(("http://localhost", "http://127.0.0.1"))
            or "code=" in message_text
        ):
            from .handlers.manage import mcp_finish_login

            if await mcp_finish_login(update, context, message_text):
                return

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []
        mcp_deliveries: List[Dict[str, Any]] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
            mcp_deliveries=mcp_deliveries,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        approval_token = self._enter_approval_chat(context, chat.id)
        success = True
        try:
            # Inject the active /goal as a prompt prefix
            goal = context.user_data.get("goal")
            effective_prompt = message_text
            if goal:
                effective_prompt = f"[Session goal] {goal}\n\n{message_text}"
            claude_response = await claude_integration.run_command(
                prompt=effective_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                **self._backend_run_params(context),
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            self._exit_approval_chat(context, approval_token)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Deliver MCP-requested documents / notifications
        if mcp_deliveries:
            await self._deliver_mcp_messages(update, mcp_deliveries)

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

        # /loop auto-continue
        await self._maybe_loop(context)

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        mcp_deliveries_doc: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
            mcp_deliveries=mcp_deliveries_doc,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        approval_token = self._enter_approval_chat(context, chat.id)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                **self._backend_run_params(context),
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            # Deliver MCP-requested documents / notifications
            if mcp_deliveries_doc:
                await self._deliver_mcp_messages(update, mcp_deliveries_doc)

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()
            self._exit_approval_chat(context, approval_token)

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        mcp_deliveries_media: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
            mcp_deliveries=mcp_deliveries_media,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        approval_token = self._enter_approval_chat(context, chat.id)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                **self._backend_run_params(context),
            )
        finally:
            heartbeat.cancel()
            self._exit_approval_chat(context, approval_token)

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        # Deliver MCP-requested documents / notifications
        if mcp_deliveries_media:
            await self._deliver_mcp_messages(update, mcp_deliveries_media)

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def interrupt_user(self, user_id: int) -> bool:
        """Interrupt the user's active request if one is running.

        Used by both the inline Stop button and the /stop command.
        Returns True if a request was interrupted.
        """
        active = self._active_requests.get(user_id)
        if active is None or active.interrupted:
            return False
        active.interrupt_event.set()
        active.interrupted = True
        return True

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if active is None:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        await self.interrupt_user(target_user_id)
        await query.answer("Stopping...", show_alert=False)

        try:
            if active.progress_msg is not None:
                await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _handle_backend_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle backend:set:<name> callbacks — switch backend from buttons."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] != "set":
            return
        name = parts[2].strip().lower()
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await query.edit_message_text("Claude integration not available.")
            return
        if name not in claude_integration.router.available_backends():
            await query.edit_message_text(
                f"❌ Unknown backend <code>{escape_html(name)}</code>.",
                parse_mode="HTML",
            )
            return
        context.user_data["backend"] = name
        context.user_data["claude_session_id"] = None
        context.user_data["force_new_session"] = True
        await query.edit_message_text(
            f"🧠 Backend switched to <b>{claude_integration.backend_display(name)}</b>.\n"
            "Session reset — your next message starts fresh.",
            parse_mode="HTML",
        )
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="backend",
                args=[name],
                success=True,
            )

    async def _handle_page_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cap:page:<kind>:<page>[:keyword] — paginated list navigation."""
        from .handlers.capabilities import (
            _page_keyboard,
            render_plugins_pages,
            render_skills_pages,
        )

        query = update.callback_query
        parts = (query.data or "").split(":")
        kind = parts[2] if len(parts) > 2 else ""
        try:
            page = int(parts[3])
        except (IndexError, ValueError):
            page = 0
        keyword = ":".join(parts[4:])
        settings: Settings = context.bot_data["settings"]
        try:
            if kind == "skills":
                text, total = render_skills_pages(
                    settings,
                    keyword,
                    page,
                    context.user_data.get("current_directory"),
                )
                kb_keyword = keyword
            elif kind == "plugins":
                text, total = render_plugins_pages(settings, page)
                kb_keyword = ""
            else:
                await query.answer("Unknown page")
                return
            keyboard = _page_keyboard(kind, page, total, kb_keyword)
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Page callback failed", error=str(exc))
        await query.answer()

    async def _handle_noop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Acknowledge disabled page-indicator buttons (cap:noop)."""
        await update.callback_query.answer()

    async def _handle_cancel_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cancel:<context> — dismiss the inline keyboard."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        try:
            if data.startswith("cancel:mng"):
                await query.edit_message_text("❌ Cancelled.", reply_markup=None)
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # pragma: no cover - message may be gone
            pass

    async def _maybe_loop(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Schedule the next auto-continue turn if /loop is active."""
        remaining = context.user_data.get("loop_remaining")
        if not remaining or remaining <= 0:
            return
        context.user_data["loop_remaining"] = remaining - 1
        if remaining - 1 <= 0:
            context.user_data.pop("loop_remaining", None)
            return
        job_queue = context.job_queue
        if job_queue is None:  # pragma: no cover - tests without job queue
            return
        job_queue.run_once(
            self._loop_continue,
            when=0.5,
            chat_id=getattr(context, "chat_id", None),
            user_id=getattr(context, "user_id", None),
            name=f"loop:{getattr(context, 'chat_id', None)}",
        )

    async def _loop_continue(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:  # pragma: no cover - exercised via jobs in live bot
        """Run one 'continue' turn for an active /loop."""
        remaining = context.user_data.get("loop_remaining")
        if not remaining or remaining <= 0:
            return
        chat_id = getattr(context, "chat_id", None)
        try:
            integration = context.bot_data.get("claude_integration")
            settings: Settings = context.bot_data["settings"]
            if not integration:
                return
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            current_dir = context.user_data.get(
                "current_directory", settings.approved_directory
            )
            resp = await integration.run_command(
                prompt="continue",
                working_directory=current_dir,
                user_id=getattr(context, "user_id", 0),
                session_id=context.user_data.get("claude_session_id"),
                interrupt_event=asyncio.Event(),
            )
            context.user_data["claude_session_id"] = resp.session_id
            content = (resp.content or "").strip()
            if content:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=content[:3900],
                    disable_web_page_preview=True,
                )
            context.user_data["loop_remaining"] = remaining - 1
            if remaining - 1 > 0:
                context.job_queue.run_once(
                    self._loop_continue,
                    when=1.0,
                    chat_id=chat_id,
                    user_id=getattr(context, "user_id", 0),
                    name=f"loop:{chat_id}",
                )
            else:
                context.user_data.pop("loop_remaining", None)
                await context.bot.send_message(
                    chat_id=chat_id, text="🔁 Loop finished."
                )
        except Exception as exc:
            context.user_data.pop("loop_remaining", None)
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=f"⚠️ Loop stopped: {exc}"
                )
            except Exception:  # pragma: no cover
                pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
