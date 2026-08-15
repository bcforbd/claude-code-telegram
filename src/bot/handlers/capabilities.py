"""Capability inspection commands: /plugins, /skills, /features (+ pagination)."""

import json
import math
from pathlib import Path
from typing import List, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...config.settings import Settings
from ..utils.html_format import escape_html

logger = structlog.get_logger()

# Pagination: keep every page comfortably under Telegram's 4096-char limit.
PAGE_SIZE = 12


def _args_list(context) -> List[str]:
    args = getattr(context, "args", None)
    return list(args) if isinstance(args, list) else []


def _config_dir(settings: Settings) -> Optional[Path]:
    """Resolve the Claude Code config directory (CLAUDE_CONFIG_DIR)."""
    return settings.claude_config_dir


def _page_keyboard(
    kind: str, page: int, total_pages: int, keyword: str = ""
) -> Optional[InlineKeyboardMarkup]:
    """Build ◀️ / page indicator / ▶️ buttons; None when nothing to paginate."""
    if total_pages <= 1:
        return None
    suffix = f":{keyword}" if keyword else ""
    row: List[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "◀️", callback_data=f"cap:page:{kind}:{page - 1}{suffix}"
            )
        )
    else:
        row.append(InlineKeyboardButton("⏹", callback_data="cap:noop"))
    row.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="cap:noop")
    )
    if page < total_pages - 1:
        row.append(
            InlineKeyboardButton(
                "▶️", callback_data=f"cap:page:{kind}:{page + 1}{suffix}"
            )
        )
    else:
        row.append(InlineKeyboardButton("⏹", callback_data="cap:noop"))
    return InlineKeyboardMarkup(
        [row, [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{kind}")]]
    )


def _clamp_page(page: int, total_pages: int) -> int:
    return max(0, min(page, total_pages - 1))


# ---------------------------------------------------------------------------
# /plugins
# ---------------------------------------------------------------------------


def _plugin_manifest_name(plugin_dir: Path) -> Optional[str]:
    """Return the plugin display name from its manifest, if any."""
    for candidate in (
        plugin_dir / ".claude-plugin" / "plugin.json",
        plugin_dir / "plugin.json",
    ):
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                name = data.get("name") or data.get("displayName")
                if name:
                    return str(name)
        except (OSError, ValueError):
            continue
    return None


def _discover_plugins(settings: Settings) -> List[dict]:
    """Resolve configured plugin dirs and list plugin folders inside them."""
    found: List[dict] = []
    for base in settings.cowork_plugin_dirs or []:
        base = Path(base)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            name = _plugin_manifest_name(child) or child.name
            found.append({"name": name, "path": str(child)})
    return found


def _installed_plugins(config_dir: Optional[Path]) -> List[dict]:
    """Read Claude Code installed_plugins.json -> [{name, version}]."""
    if not config_dir:
        return []
    manifest = config_dir / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    out: List[dict] = []
    for name, entries in plugins.items():
        version = ""
        if isinstance(entries, list) and entries:
            version = str(entries[0].get("version") or "")
        out.append({"name": name, "version": version})
    return sorted(out, key=lambda p: p["name"].lower())


def collect_plugins(settings: Settings) -> List[dict]:
    """Flat plugin list: {name, detail, kind} (installed + discovered)."""
    items: List[dict] = []
    cfg_dir = _config_dir(settings)
    if cfg_dir:
        for p in _installed_plugins(cfg_dir):
            items.append(
                {
                    "name": p["name"],
                    "detail": f"v{p['version']}" if p["version"] else "",
                    "kind": "✅ Installed",
                }
            )
    for p in _discover_plugins(settings):
        items.append({"name": p["name"], "detail": p["path"], "kind": "🔎 Discovered"})
    return items


def render_plugins_pages(settings: Settings, page: int) -> Tuple[str, int]:
    """Render one /plugins page -> (html_text, total_pages)."""
    items = collect_plugins(settings)
    total = len(items)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = _clamp_page(page, total_pages)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [
        "🧩 <b>Plugins</b>",
        f"<b>Total: {total}</b> · page {page + 1}/{total_pages}",
    ]
    if not chunk:
        lines.append("— none —")
    for p in chunk:
        detail = f" — <code>{escape_html(p['detail'])}</code>" if p["detail"] else ""
        lines.append(f"{p['kind']} <b>{escape_html(p['name'])}</b>{detail}")
    if not items and not _config_dir(settings):
        lines.append(
            "\n💡 Set <code>CLAUDE_CONFIG_DIR</code> in .env to see installed "
            "plugins, or <code>COWORK_PLUGIN_DIRS=/path/to/plugins</code>."
        )
    return "\n".join(lines), total_pages


async def plugins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List installed Claude Code plugins + configured plugin directories."""
    settings: Settings = context.bot_data["settings"]
    page = 0
    text, total_pages = render_plugins_pages(settings, page)
    kwargs = {"parse_mode": "HTML"}
    keyboard = _page_keyboard("plugins", page, total_pages)
    if keyboard:
        kwargs["reply_markup"] = keyboard
    await update.message.reply_text(text, **kwargs)


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------


def _scan_skills(root: Path) -> List[dict]:
    """Find SKILL.md files under <root>/<skill>/SKILL.md and summarize them."""
    skills: List[dict] = []
    if not root.is_dir():
        return skills
    for skill_dir in sorted(root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.is_file():
            continue
        description = ""
        try:
            head = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in head[:12]:
                stripped = line.strip()
                if stripped.lower().startswith("description:"):
                    description = stripped.split(":", 1)[1].strip()
                    break
                if stripped.startswith("#"):
                    description = stripped.lstrip("#").strip()
                    break
        except OSError:
            pass
        skills.append({"name": skill_dir.name, "description": description})
    return skills


def _skills_roots(
    settings: Settings, current_dir: Optional[str]
) -> List[Tuple[str, Path]]:
    """Ordered skill roots: config dir, user, project, current dir."""
    roots: List[Tuple[str, Path]] = []
    cfg_dir = _config_dir(settings)
    if cfg_dir:
        roots.append(("🧠 AI-Tools", cfg_dir / "skills"))
    roots.append(("👤 User", Path.home() / ".claude" / "skills"))
    roots.append(("📁 Project", settings.approved_directory / ".claude" / "skills"))
    if current_dir is not None:
        roots.append(("📍 Current", Path(current_dir) / ".claude" / "skills"))
    return roots


def collect_skills(
    settings: Settings, current_dir: Optional[str], keyword: str
) -> List[dict]:
    """Flat, deduped skill list: {name, description, scope}."""
    items: List[dict] = []
    seen = set()
    for label, root in _skills_roots(settings, current_dir):
        for s in _scan_skills(root):
            if s["name"] in seen:
                continue
            seen.add(s["name"])
            if keyword and not (
                keyword in s["name"].lower() or keyword in s["description"].lower()
            ):
                continue
            items.append(
                {"name": s["name"], "description": s["description"], "scope": label}
            )
    return items


def render_skills_pages(
    settings: Settings,
    keyword: str,
    page: int,
    current_dir: Optional[str] = None,
) -> Tuple[str, int]:
    """Render one /skills page -> (html_text, total_pages)."""
    items = collect_skills(settings, current_dir, keyword)
    total = len(items)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = _clamp_page(page, total_pages)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = ["🛠️ <b>Skills</b>"]
    if keyword:
        lines.append(f"Filter: <code>{escape_html(keyword)}</code>")
    lines.append(f"<b>Total: {total}</b> · page {page + 1}/{total_pages}")
    if not chunk:
        lines.append("— none —")
    for s in chunk:
        desc = s["description"]
        if len(desc) > 80:
            desc = desc[:80] + "…"
        suffix = f" — {escape_html(desc)}" if desc else ""
        lines.append(f"{s['scope']} <b>{escape_html(s['name'])}</b>{suffix}")
    if page == 0 and not keyword:
        lines.append(
            "\n<code>/skills &lt;keyword&gt;</code> filters · ◀️ ▶️ to browse pages"
        )
    return "\n".join(lines), total_pages


async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List Claude Code skills (config dir + user + project + current scope)."""
    settings: Settings = context.bot_data["settings"]
    keyword = " ".join(_args_list(context)).strip().lower()
    page = 0
    text, total_pages = render_skills_pages(
        settings, keyword, page, context.user_data.get("current_directory")
    )
    kwargs = {"parse_mode": "HTML"}
    keyboard = _page_keyboard("skills", page, total_pages, keyword)
    if keyboard:
        kwargs["reply_markup"] = keyboard
    await update.message.reply_text(text, **kwargs)


# ---------------------------------------------------------------------------
# MCP aggregation (shared by /mcp in agentic + classic modes)
# ---------------------------------------------------------------------------


def collect_mcp_servers(settings: Settings) -> List[Tuple[str, List[str]]]:
    """Aggregate real MCP servers from all configured sources.

    Bot-config entries are annotated with transport + enabled state.
    """
    sources: List[Tuple[str, List[str]]] = []

    cfg_dir = _config_dir(settings)
    if cfg_dir:
        try:
            data = json.loads((cfg_dir / "settings.json").read_text(encoding="utf-8"))
            servers = sorted((data.get("mcpServers") or {}).keys())
            if servers:
                sources.append(("Claude Code settings.json", servers))
        except (OSError, ValueError):
            pass

    desktop_cfg = (
        Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    )
    try:
        data = json.loads(desktop_cfg.read_text(encoding="utf-8"))
        servers = sorted((data.get("mcpServers") or {}).keys())
        if servers:
            sources.append(("Claude Desktop", servers))
    except (OSError, ValueError):
        pass

    if settings.mcp_config_path:
        try:
            data = json.loads(settings.mcp_config_path.read_text(encoding="utf-8"))
            for name, entry in sorted((data.get("mcpServers") or {}).items()):
                enabled = entry.get("enabled", True) is not False
                t = entry.get("type", "stdio")
                mark = "🟢" if enabled else "⚪"
                transport = {"http": "HTTP", "sse": "SSE"}.get(t, "stdio")
                sources.append((f"Bot MCP config ({transport})", [f"{mark} {name}"]))
        except (OSError, ValueError):
            pass

    return sources


async def mcp_command_shim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the full /mcp listing without requiring an active backend."""
    settings: Settings = context.bot_data["settings"]
    sources = collect_mcp_servers(settings)
    if not sources:
        await update.message.reply_text(
            "🔌 No MCP servers found. Set <code>CLAUDE_CONFIG_DIR</code> or "
            "<code>ENABLE_MCP=true</code> + <code>MCP_CONFIG_PATH</code>.\n\n"
            "Manage with <code>/mcp add &lt;name&gt; &lt;command&gt; [args...]</code>.",
            parse_mode="HTML",
        )
        return
    lines = ["🔌 <b>MCP servers</b>"]
    for label, servers in sources:
        lines.append(f"\n🧩 <b>{escape_html(label)}</b>:")
        lines.append("\n".join(f"• <code>{s}</code>" for s in servers))
    lines.append(
        "\nManage: <code>/mcp add &lt;name&gt; &lt;command&gt; [args...]</code> "
        "· <code>/mcp remove &lt;name&gt;</code>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def format_mcp_reply(
    active_display: str, backend_servers: List[str], settings: Settings
) -> str:
    """Build the /mcp reply: backend servers + all discovered sources."""
    lines = [f"🔌 MCP servers for <b>{active_display}</b>:"]
    if backend_servers:
        lines.append("\n".join(f"• <code>{s}</code>" for s in backend_servers))
    else:
        lines.append("None configured.")

    sources = collect_mcp_servers(settings)
    if sources:
        for label, servers in sources:
            lines.append(f"\n🧩 <b>{escape_html(label)}</b>:")
            lines.append("\n".join(f"• <code>{s}</code>" for s in servers))
    else:
        lines.append(
            "\n💡 No MCP servers found. Set <code>CLAUDE_CONFIG_DIR</code> or "
            "<code>ENABLE_MCP=true</code> + <code>MCP_CONFIG_PATH</code>."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /features
# ---------------------------------------------------------------------------


async def features_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show which optional features are enabled and how to turn them on."""
    settings: Settings = context.bot_data["settings"]
    from ...config.features import FeatureFlags

    flags = FeatureFlags(settings)
    enabled = flags.get_enabled_features()

    lines = ["⚙️ <b>Enabled Features</b>"]
    if enabled:
        lines.append("\n".join(f"• <code>{f}</code>" for f in sorted(enabled)))
    else:
        lines.append("— none —")

    lines.append("\n<b>Toggles (in .env):</b>")
    toggles = [
        ("scheduler", settings.enable_scheduler, "ENABLE_SCHEDULER=true"),
        ("api_server", settings.enable_api_server, "ENABLE_API_SERVER=true"),
        ("mcp", settings.enable_mcp, "ENABLE_MCP=true + MCP_CONFIG_PATH=..."),
        (
            "project_threads",
            settings.enable_project_threads,
            "ENABLE_PROJECT_THREADS=true",
        ),
        ("git", settings.enable_git_integration, "ENABLE_GIT_INTEGRATION=true"),
        ("file_uploads", settings.enable_file_uploads, "ENABLE_FILE_UPLOADS=true"),
        (
            "remote_approvals",
            settings.enable_remote_approvals,
            "ENABLE_REMOTE_APPROVALS=true",
        ),
    ]
    for name, on, how in toggles:
        lines.append(f"{'✅' if on else '⬜'} <code>{name}</code> — {how}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
