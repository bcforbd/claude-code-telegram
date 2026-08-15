"""Claude Desktop backend.

Mirrors the Claude Desktop app by driving the Claude Code engine with the
Desktop app's own configuration: MCP servers and env (including any
developer-mode gateway routing such as ``ANTHROPIC_BASE_URL``) imported from
``claude_desktop_config.json``.

Claude Desktop's "developer mode" is a config-file surface, not an inbound
HTTP API — importing that config is exactly what makes the bot behave like
"Claude Desktop with developer mode on".
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

logger = structlog.get_logger()


class ClaudeDesktopManager:
    """Backend that executes the agent with Claude Desktop's configuration."""

    name = "desktop"

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
    ) -> None:
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self._desktop_config: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Config discovery / parsing
    # ------------------------------------------------------------------

    def desktop_config_path(self) -> Path:
        """Resolve the claude_desktop_config.json path."""
        if self.config.desktop_config_path:
            return self.config.desktop_config_path
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"

    def load_desktop_config(self) -> Optional[Dict[str, Any]]:
        """Load (and cache) the Claude Desktop config; None if unavailable."""
        if self._desktop_config is not None:
            return self._desktop_config
        path = self.desktop_config_path()
        if not path.exists():
            logger.warning("Claude Desktop config not found", path=str(path))
            return None
        try:
            with open(path, encoding="utf-8") as f:
                self._desktop_config = json.load(f)
            logger.info("Loaded Claude Desktop config", path=str(path))
            return self._desktop_config
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Failed to parse Claude Desktop config",
                path=str(path),
                error=str(exc),
            )
            return None

    def mcp_servers(self) -> Dict[str, Any]:
        """MCP servers declared in the Desktop config (valid stdio entries)."""
        cfg = self.load_desktop_config() or {}
        servers = cfg.get("mcpServers", {}) or {}
        return {
            str(name): value
            for name, value in servers.items()
            if isinstance(value, dict) and value.get("command")
        }

    def env_overrides(self) -> Dict[str, str]:
        """Env block from the Desktop config (developer-mode gateway etc.)."""
        cfg = self.load_desktop_config() or {}
        env = cfg.get("env", {}) or {}
        return {
            str(key): str(value)
            for key, value in env.items()
            if isinstance(value, (str, int, float, bool))
        }

    # ------------------------------------------------------------------
    # ClaudeBackend contract
    # ------------------------------------------------------------------

    async def status(self) -> Dict[str, Any]:
        """JSON-friendly status for /desktop and diagnostics."""
        cfg = self.load_desktop_config()
        servers = self.mcp_servers()
        env = self.env_overrides()
        return {
            "ok": cfg is not None,
            "backend": self.name,
            "config_path": str(self.desktop_config_path()),
            "config_found": cfg is not None,
            "mcp_servers": sorted(servers) if servers else [],
            "env_keys": sorted(env) if env else [],
            "gateway_base_url": env.get("ANTHROPIC_BASE_URL"),
            "developer_mode_env": any(key.startswith("ANTHROPIC_") for key in env),
            "permission_mode": self.config.default_permission_mode or "default",
        }

    def describe(self) -> str:
        return (
            "Claude Desktop mirror — imports claude_desktop_config.json "
            "(MCP servers + env, incl. developer-mode gateway routing)"
        )

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
        """Execute with the Desktop app's MCP servers + env overlay."""
        servers = self.mcp_servers()
        env = self.env_overrides()
        logger.info(
            "Desktop backend executing",
            mcp_server_count=len(servers),
            env_keys=sorted(env) if env else [],
        )
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
            mcp_servers=servers or None,
            env_overrides=env or None,
        )
