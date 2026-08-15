"""Claude Cowork backend.

Claude Cowork is a consumer GUI mode of the same agent engine that powers
Claude Code, so the programmatic equivalent is the Claude Code engine
configured the way Cowork configures it:

  - Cowork-style model default (``auto/claude-opus``, overridable)
  - Account conversation preferences loaded into the system prompt
    (this machine: ``cowork_account_settings.json``)
  - ``acceptEdits`` permission mode (≈ Cowork's Auto approval)
  - Web search enabled (matches ``coworkWebSearchEnabled``)
  - Optional Cowork plugin directories
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

logger = structlog.get_logger()

COWORK_MODEL_DEFAULT = "auto/claude-opus"
WEB_SEARCH_TOOL = "WebSearch"

# Keys that mark a Cowork profile as "real" (vs an empty bootstrap profile).
_PROFILE_CONTENT_KEYS = (
    "conversation_preferences",
    "__account_profile",
    "__model_selector_state",
    "model",
)


def _profile_score(path: Path) -> tuple:
    """Score a candidate cowork_account_settings.json for discovery.

    Prefers profiles with real content (preferences/model selector), then
    the most recently modified.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0, 0.0)
    meaningful = any(key in data for key in _PROFILE_CONTENT_KEYS)
    try:
        mtime = path.stat().st_mtime
    except OSError:  # pragma: no cover - defensive
        mtime = 0.0
    return (1 if meaningful else 0, mtime)


def _extract_preferences_text(value: Any) -> Optional[str]:
    """Extract a system-prompt string from Cowork conversation_preferences.

    Cowork stores preferences either as a plain string or as a list of
    content blocks (e.g. [{"type": "text", "content": "..."}]).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts: List[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("content") or block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "\n".join(p for p in parts if p.strip()).strip()
        return joined or None
    return None


class CoworkManager:
    """Backend that executes the agent with a Claude Cowork profile."""

    name = "cowork"

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
    ) -> None:
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self._cowork_settings: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Settings discovery / parsing
    # ------------------------------------------------------------------

    def _default_search_roots(self) -> List[Path]:
        """Candidate roots for auto-discovering Cowork settings."""
        roots: List[Path] = []
        appdata = Path.home() / "AppData" / "Roaming"
        roots.append(appdata / "Claude")
        # Local Claude Desktop 3p install (known layout on this machine).
        roots.append(Path(r"D:\AI-Dev-Data\AI-Tools\Claude-Desktop\Claude-3p"))
        return roots

    def cowork_settings_path(self) -> Optional[Path]:
        """Resolve cowork_account_settings.json; None if not found."""
        if self.config.cowork_settings_path:
            path = self.config.cowork_settings_path
            return path if path.exists() else None

        for root in self._default_search_roots():
            if not root.exists():
                continue
            direct = root / "cowork_account_settings.json"
            if direct.exists():
                return direct
            # 3p layout: local-agent-mode-sessions/<user>/<user>/cowork_account_settings.json
            sessions_root = root / "local-agent-mode-sessions"
            if sessions_root.exists():
                try:
                    matches = sorted(
                        sessions_root.glob("*/*/cowork_account_settings.json")
                    )
                except OSError:  # pragma: no cover - defensive
                    matches = []
                if matches:
                    return max(matches, key=_profile_score)
        return None

    def load_cowork_settings(self) -> Optional[Dict[str, Any]]:
        """Load (and cache) Cowork account settings; None if unavailable."""
        if self._cowork_settings is not None:
            return self._cowork_settings
        path = self.cowork_settings_path()
        if path is None:
            logger.warning("Cowork settings not found")
            return None
        try:
            with open(path, encoding="utf-8") as f:
                self._cowork_settings = json.load(f)
            logger.info("Loaded Cowork settings", path=str(path))
            return self._cowork_settings
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Failed to parse Cowork settings",
                path=str(path),
                error=str(exc),
            )
            return None

    def conversation_preferences(self) -> Optional[str]:
        """Cowork account conversation preferences (system-prompt material)."""
        cfg = self.load_cowork_settings() or {}
        prefs = _extract_preferences_text(cfg.get("conversation_preferences"))
        if prefs:
            return prefs
        # 3p layout keeps preferences under __account_profile.
        profile = cfg.get("__account_profile")
        if isinstance(profile, dict):
            prefs = _extract_preferences_text(profile.get("conversation_preferences"))
            if prefs:
                return prefs
        return None

    def account_model(self) -> Optional[str]:
        """Model selector from Cowork settings (best-effort)."""
        cfg = self.load_cowork_settings() or {}
        # 3p layout: __model_selector_state.cowork.model
        selector = cfg.get("__model_selector_state")
        if isinstance(selector, dict):
            cowork = selector.get("cowork")
            if isinstance(cowork, dict):
                model = cowork.get("model")
                if isinstance(model, str) and model.strip():
                    return model.strip()
        for key in ("model", "model_selector", "preferred_model"):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def resolved_model(self) -> str:
        """Model for cowork executions: env > account > Cowork default."""
        return self.config.cowork_model or self.account_model() or COWORK_MODEL_DEFAULT

    def plugin_dirs(self) -> List[Dict[str, str]]:
        """Plugin dirs from settings (validated to exist)."""
        dirs = self.config.cowork_plugin_dirs or []
        plugins = []
        for directory in dirs:
            if directory.exists() and directory.is_dir():
                plugins.append({"path": str(directory)})
            else:
                logger.warning("Cowork plugin dir missing", path=str(directory))
        return plugins

    # ------------------------------------------------------------------
    # ClaudeBackend contract
    # ------------------------------------------------------------------

    async def status(self) -> Dict[str, Any]:
        """JSON-friendly status for /cowork and diagnostics."""
        settings_path = self.cowork_settings_path()
        preferences = self.conversation_preferences()
        return {
            "ok": True,
            "backend": self.name,
            "settings_path": str(settings_path) if settings_path else None,
            "settings_found": settings_path is not None,
            "model": self.resolved_model(),
            "permission_mode": self.config.cowork_permission_mode,
            "web_search": self.config.cowork_enable_web_search,
            "plugin_dirs": [p["path"] for p in self.plugin_dirs()],
            "preferences_loaded": bool(preferences),
            "preferences_length": len(preferences) if preferences else 0,
        }

    def describe(self) -> str:
        return (
            "Claude Cowork mirror — Cowork model, conversation preferences "
            "system prompt, acceptEdits permissions, web search, plugins"
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
        """Execute with the Cowork profile (preferences + model + web search)."""
        preferences = self.conversation_preferences()
        system_prompt_extra = None
        if preferences:
            system_prompt_extra = (
                "Follow the user's Cowork conversation preferences:\n\n"
                f"{preferences}"
            )

        allowed_tools_extra = (
            [WEB_SEARCH_TOOL] if self.config.cowork_enable_web_search else None
        )

        logger.info(
            "Cowork backend executing",
            model=self.resolved_model(),
            permission_mode=self.config.cowork_permission_mode,
            preferences_loaded=bool(preferences),
            web_search=self.config.cowork_enable_web_search,
            plugin_count=len(self.plugin_dirs()),
        )
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            interrupt_event=interrupt_event,
            images=images,
            model=model or self.resolved_model(),
            permission_mode=permission_mode or self.config.cowork_permission_mode,
            system_prompt_extra=system_prompt_extra,
            plugins=self.plugin_dirs() or None,
            allowed_tools_extra=allowed_tools_extra,
        )
