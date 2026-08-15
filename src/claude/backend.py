"""Backend abstraction for Claude engine profiles.

A "backend" is a named way to configure and drive the Claude Code agent
engine.  All backends share one contract (``ClaudeBackend``) so the bot can
switch between plain Claude Code (``sdk``), a Claude Desktop mirror
(``desktop``), and a Claude Cowork mirror (``cowork``) per chat or globally.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

import structlog

from ..config.settings import Settings
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

logger = structlog.get_logger()

BACKEND_NAMES: tuple = ("sdk", "desktop", "cowork")
BACKEND_DISPLAY: Dict[str, str] = {
    "sdk": "Claude Code (SDK)",
    "desktop": "Claude Desktop mirror",
    "cowork": "Claude Cowork mirror",
}


class SdkBackend:
    """Adapter exposing the base ClaudeSDKManager as a ClaudeBackend."""

    name = "sdk"

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
    ) -> None:
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
    ) -> ClaudeResponse:
        """Pass through to the SDK manager."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            interrupt_event=interrupt_event,
            images=images,
            model=model,
            permission_mode=permission_mode or self.config.default_permission_mode,
        )

    async def status(self) -> Dict[str, Any]:
        """Status snapshot for the plain SDK backend."""
        return {
            "ok": True,
            "backend": self.name,
            "model": self.config.claude_model,
            "permission_mode": self.config.default_permission_mode or "default",
            "mcp_enabled": self.config.enable_mcp,
            "mcp_config_path": (
                str(self.config.mcp_config_path)
                if self.config.mcp_config_path
                else None
            ),
        }

    def describe(self) -> str:
        return "Claude Code via claude-agent-sdk (default)"


@runtime_checkable
class ClaudeBackend(Protocol):
    """Contract every Claude backend must implement."""

    name: str

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute a prompt against the backend and return a ClaudeResponse."""
        ...

    async def status(self) -> Dict[str, Any]:
        """Return a JSON-friendly status snapshot for /status and diagnostics."""
        ...

    def describe(self) -> str:
        """Short human-readable backend description."""
        ...


class BackendRouter:
    """Selects the active backend for a given chat.

    Resolution order:
      1. Per-chat override (``context.user_data["backend"]``) if valid
      2. ``Settings.claude_backend``
      3. ``sdk`` (hard fallback)
    """

    def __init__(
        self,
        config: Settings,
        backends: Dict[str, ClaudeBackend],
    ) -> None:
        self.config = config
        self.backends = backends

    def available_backends(self) -> List[str]:
        """Names of registered backends, in canonical order."""
        return [name for name in BACKEND_NAMES if name in self.backends]

    def select(self, chat_override: Optional[str] = None) -> ClaudeBackend:
        """Pick the backend to use for the current request."""
        candidate = chat_override or self.config.claude_backend
        backend = self.backends.get(candidate)
        if backend is None:
            if candidate != self.config.claude_backend:
                logger.warning(
                    "Unknown chat backend override, falling back to config",
                    override=candidate,
                    default=self.config.claude_backend,
                )
            backend = self.backends.get(self.config.claude_backend)
        if backend is None:
            logger.warning("Configured backend missing, falling back to sdk")
            backend = self.backends.get("sdk")
        if backend is None:
            raise RuntimeError("No Claude backend registered (sdk backend missing)")
        return backend

    async def status(self) -> Dict[str, Any]:
        """Status of every registered backend (for /status and /desktop)."""
        result: Dict[str, Any] = {}
        for name in self.available_backends():
            backend = self.backends[name]
            try:
                result[name] = await backend.status()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Backend status failed", backend=name, error=str(exc))
                result[name] = {"ok": False, "error": str(exc)}
        return result
