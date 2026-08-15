"""Management commands: plugin/skill/MCP management + Claude-style workflow commands.

Provides:
- /plugins install|remove|update|marketplace ...  (wraps the `claude plugin` CLI)
- /skills create|show|delete|list|search
- /mcp add|remove|list                        (manages the bot's MCP config)
- /goal, /loop, /compact, /clear, /review, /todo, /memory, /hooks, /config, /plan
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote_plus, unquote_plus

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...config.settings import Settings
from ..utils.html_format import escape_html

logger = structlog.get_logger()

MAX_REPLY = 3500


def _truncate_text(text: str, limit: int = MAX_REPLY) -> str:
    if len(text) <= limit:
        return text
    return (
        text[: limit - 40]
        + "\n\n… (truncated, "
        + str(len(text) - limit)
        + " chars hidden)"
    )


# ---------------------------------------------------------------------------
# claude CLI runner
# ---------------------------------------------------------------------------


def _claude_path() -> Optional[str]:
    return shutil.which("claude")


def run_claude_cli(
    args: List[str], settings: Settings, timeout: int = 120
) -> Tuple[int, str]:
    """Run a `claude` CLI subcommand, capturing output."""
    claude = _claude_path()
    if not claude:
        return 1, "`claude` CLI not found on PATH."
    env = os.environ.copy()
    if settings.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(settings.claude_config_dir)
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [claude, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=create_no_window,
        )
        out = (proc.stdout or "").strip()
        if proc.stderr and proc.stderr.strip():
            out = (out + "\n" + proc.stderr.strip()).strip()
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 1, f"Command timed out after {timeout}s."
    except Exception as exc:  # pragma: no cover - defensive
        return 1, f"Failed to run claude CLI: {exc}"


async def _reply_cli_result(update: Update, rc: int, output: str, action: str) -> None:
    icon = "✅" if rc == 0 else "❌"
    text = f"{icon} <b>{escape_html(action)}</b>\n"
    if output:
        text += f"<pre>{escape_html(_truncate_text(output))}</pre>"
    else:
        text += "Done."
    await update.message.reply_text(text, parse_mode="HTML")


def _confirm_keyboard(op: str, payload: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm",
                    callback_data=f"mng:confirm:{op}:{quote_plus(payload)}",
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel:mng"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# /plugins — management subcommands
# ---------------------------------------------------------------------------


def _run_plugin_op(op: str, payload: str, settings: Settings) -> Tuple[int, str]:
    """Re-run a plugin op after confirmation (payload is the joined args)."""
    args: List[str] = []
    if op == "install":
        args = ["plugin", "install", "-y", payload]
    elif op == "uninstall":
        args = ["plugin", "uninstall", "-y", payload]
    elif op == "update":
        args = ["plugin", "update"] + ([payload] if payload else [])
    elif op == "marketplace_add":
        args = ["plugin", "marketplace", "add", payload]
    elif op == "marketplace_remove":
        args = ["plugin", "marketplace", "remove", payload]
    elif op == "marketplace_update":
        args = ["plugin", "marketplace", "update"] + ([payload] if payload else [])
    else:  # pragma: no cover - defensive
        return 1, f"Unknown plugin op: {op}"
    return run_claude_cli(args, settings)


async def plugins_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plugins [install|remove|update|list|marketplace ...] — manage plugins."""
    settings: Settings = context.bot_data["settings"]
    args = (update.message.text or "").split()[1:]

    if not args:
        from .capabilities import plugins_command

        await plugins_command(update, context)
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub in ("install", "i") and rest:
        plugin = " ".join(rest)
        rc, out = _run_plugin_op("install", plugin, settings)
        await _reply_cli_result(update, rc, out, f"Installing plugin {plugin}")
        return
    if sub in ("remove", "uninstall", "rm") and rest:
        plugin = " ".join(rest)
        await update.message.reply_text(
            f"⚠️ Uninstall <b>{escape_html(plugin)}</b>?",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("uninstall", plugin),
        )
        return
    if sub == "update":
        plugin = " ".join(rest)
        rc, out = _run_plugin_op("update", plugin, settings)
        await _reply_cli_result(update, rc, out, f"Updating plugin {plugin or '(all)'}")
        return
    if sub == "list":
        rc, out = run_claude_cli(["plugin", "list"], settings)
        await _reply_cli_result(update, rc, out, "Installed plugins (CLI)")
        return
    if sub in ("enable", "disable") and rest:
        rc, out = run_claude_cli(["plugin", sub, rest[0]], settings)
        await _reply_cli_result(update, rc, out, f"{sub.capitalize()} plugin {rest[0]}")
        return
    if sub == "marketplace" or sub == "mp":
        return await _plugins_marketplace(update, context, rest)

    await update.message.reply_text(
        "🧩 <b>Plugin Management</b>\n\n"
        "<code>/plugins install &lt;name&gt;</code> — install from marketplaces\n"
        "<code>/plugins remove &lt;name&gt;</code> — uninstall (asks confirm)\n"
        "<code>/plugins update [name]</code> — update plugin(s)\n"
        "<code>/plugins list</code> — CLI install list\n"
        "<code>/plugins marketplace add &lt;url|path&gt;</code>\n"
        "<code>/plugins marketplace remove &lt;name&gt;</code>\n"
        "<code>/plugins marketplace update [name]</code>\n"
        "<code>/plugins marketplace list</code>",
        parse_mode="HTML",
    )


async def _plugins_marketplace(
    update: Update, context: ContextTypes.DEFAULT_TYPE, rest: List[str]
) -> None:
    settings: Settings = context.bot_data["settings"]
    sub = rest[0].lower() if rest else "list"
    rest = rest[1:]

    if sub == "add" and rest:
        source = " ".join(rest)
        rc, out = _run_plugin_op("marketplace_add", source, settings)
        await _reply_cli_result(update, rc, out, f"Adding marketplace {source}")
        return
    if sub in ("remove", "rm") and rest:
        name = rest[0]
        await update.message.reply_text(
            f"⚠️ Remove marketplace <b>{escape_html(name)}</b>?",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("marketplace_remove", name),
        )
        return
    if sub == "update":
        name = rest[0] if rest else ""
        rc, out = _run_plugin_op("marketplace_update", name, settings)
        await _reply_cli_result(
            update, rc, out, f"Updating marketplace {name or '(all)'}"
        )
        return
    # list (default)
    rc, out = run_claude_cli(["plugin", "marketplace", "list"], settings)
    await _reply_cli_result(update, rc, out, "Marketplaces")


async def mng_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle mng:confirm:<op>:<payload> — confirm a pending management action."""
    settings: Settings = context.bot_data["settings"]
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":", 3)
    if len(parts) < 4:
        await query.answer("Invalid confirmation")
        return
    op = parts[2]
    payload = unquote_plus(parts[3])

    if op == "skill_delete":
        import shutil as _shutil

        target = _skills_root(settings) / payload
        try:
            _shutil.rmtree(target)
            text = f"🗑️ Deleted skill <b>{escape_html(payload)}</b>."
        except Exception as exc:
            text = f"❌ Failed to delete skill: {escape_html(str(exc))}"
    else:
        rc, out = _run_plugin_op(op, payload, settings)
        icon = "✅" if rc == 0 else "❌"
        text = f"{icon} <b>{escape_html(op)} → {escape_html(payload)}</b>\n"
        text += f"<pre>{escape_html(_truncate_text(out))}</pre>" if out else "Done."
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except Exception:  # pragma: no cover - message may have been deleted
        await query.answer("Done")
    await query.answer()


# ---------------------------------------------------------------------------
# /skills — management subcommands
# ---------------------------------------------------------------------------


def _skills_root(settings: Settings) -> Path:
    if settings.claude_config_dir:
        return settings.claude_config_dir / "skills"
    return Path.home() / ".claude" / "skills"


def _valid_skill_name(name: str) -> bool:
    return (
        bool(name) and all(c.isalnum() or c in "-_" for c in name) and name[0].isalnum()
    )


async def skills_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skills [create|show|delete|list|search <kw>] — manage skills."""
    from .capabilities import skills_command

    settings: Settings = context.bot_data["settings"]
    args = (update.message.text or "").split()[1:]

    if not args:
        await skills_command(update, context)
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub == "create" and rest:
        name = rest[0].lower()
        description = " ".join(rest[1:]) or "TBD"
        if not _valid_skill_name(name):
            await update.message.reply_text(
                "❌ Invalid skill name. Use letters, digits, <code>-</code>, <code>_</code>.",
                parse_mode="HTML",
            )
            return
        root = _skills_root(settings)
        target = root / name
        if (target / "SKILL.md").exists():
            await update.message.reply_text(
                f"⚠️ Skill <b>{escape_html(name)}</b> already exists.",
                parse_mode="HTML",
            )
            return
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n"
            f"# {name}\n\nDescribe what this skill does and how to use it.\n",
            encoding="utf-8",
        )
        await update.message.reply_text(
            f"✅ Created skill <b>{escape_html(name)}</b>\n"
            f"<code>{escape_html(str(target / 'SKILL.md'))}</code>\n\n"
            "Edit the file to add instructions.",
            parse_mode="HTML",
        )
        return

    if sub == "show" and rest:
        name = rest[0].lower()
        md = _skills_root(settings) / name / "SKILL.md"
        if not md.is_file():
            await update.message.reply_text(
                f"❌ Skill <b>{escape_html(name)}</b> not found.", parse_mode="HTML"
            )
            return
        content = md.read_text(encoding="utf-8", errors="replace")
        await update.message.reply_text(
            f"📄 <b>{escape_html(name)}</b>/SKILL.md\n\n"
            f"<pre>{escape_html(_truncate_text(content))}</pre>",
            parse_mode="HTML",
        )
        return

    if sub == "delete" and rest:
        name = rest[0].lower()
        if not (_skills_root(settings) / name / "SKILL.md").is_file():
            await update.message.reply_text(
                f"❌ Skill <b>{escape_html(name)}</b> not found.", parse_mode="HTML"
            )
            return
        await update.message.reply_text(
            f"⚠️ Delete skill <b>{escape_html(name)}</b> and its files?",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("skill_delete", name),
        )
        return

    if sub == "list":
        await skills_command(update, context)
        return

    # Default: keyword filter (e.g. /skills pdf)
    await skills_command(update, context)


async def mng_skill_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:  # pragma: no cover - legacy
    """Legacy direct skill-delete callback (superseded by mng_confirm)."""
    await mng_confirm(update, context)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /mcp — full connector management (stdio/SSE/HTTP + OAuth + enable/disable)
# ---------------------------------------------------------------------------


def _mcp_config_path(settings: Settings) -> Path:
    if settings.mcp_config_path:
        return settings.mcp_config_path
    return Path("config") / "mcp.json"


def load_mcp_config(settings: Settings) -> Dict[str, Any]:
    path = _mcp_config_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"mcpServers": {}}


def save_mcp_config(settings: Settings, data: Dict[str, Any]) -> Path:
    path = _mcp_config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _server_transport(entry: Dict[str, Any]) -> str:
    """Human label for an MCP server's transport type."""
    t = entry.get("type", "stdio")
    if t == "http":
        return "🌐 HTTP (streamable)"
    if t == "sse":
        return "📡 SSE"
    return "💻 stdio"


def _parse_headers(tokens: List[str]) -> Dict[str, str]:
    """Recombine "Header: value with spaces" tokens split by message.split()."""
    headers: Dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if ":" in tok:
            key, _, val = tok.partition(":")
            val = val.lstrip()
            i += 1
            while i < len(tokens) and ":" not in tokens[i]:
                val = (val + " " + tokens[i]).strip()
                i += 1
            headers[key.strip()] = val
        else:
            i += 1
    return headers


def _server_entry(settings: Settings, name: str) -> Optional[Dict[str, Any]]:
    return load_mcp_config(settings).get("mcpServers", {}).get(name)


def _sync_to_claude_cli(
    name: str, entry: Dict[str, Any], settings: Settings
) -> Tuple[int, str]:
    """Best-effort mirror of a bot-config server into the Claude Code user config."""
    t = entry.get("type", "stdio")
    if t in ("http", "sse"):
        args = ["mcp", "add", "--transport", t, name, entry.get("url", "")]
        for header in entry.get("headers", {}).items():
            args += ["--header", f"{header[0]}: {header[1]}"]
    else:
        args = ["mcp", "add", name, entry.get("command", ""), *entry.get("args", [])]
    return run_claude_cli(args, settings, timeout=60)


def _mcp_manage_keyboard(
    servers: Dict[str, Any], page: int = 0
) -> InlineKeyboardMarkup:
    """One row of action buttons per server (connect/auth/toggle/remove)."""
    rows: List[List[InlineKeyboardButton]] = []
    names = sorted(servers.keys())
    for name in names:
        entry = servers[name]
        enabled = entry.get("enabled", True) is not False
        toggle = "⛔ Disable" if enabled else "✅ Enable"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{toggle}", callback_data=f"mng:mcp:toggle:{name}"
                ),
                InlineKeyboardButton(
                    "🔌 Connect", callback_data=f"mng:mcp:connect:{name}"
                ),
                InlineKeyboardButton("🔑 Auth", callback_data=f"mng:mcp:auth:{name}"),
                InlineKeyboardButton(
                    "🗑 Remove", callback_data=f"mng:mcp:remove:{name}"
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="mng:mcp:refresh"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel:mng"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _format_mcp_list(settings: Settings) -> str:
    """Rich /mcp listing with transport + enabled state for every server."""
    data = load_mcp_config(settings)
    servers = data.get("mcpServers", {})
    lines = ["🔌 <b>MCP Servers</b>"]
    if not servers:
        lines.append("— none configured —")
    for name in sorted(servers):
        entry = servers[name]
        enabled = entry.get("enabled", True) is not False
        dot = "🟢" if enabled else "⚪"
        transport = _server_transport(entry)
        lines.append(f"{dot} <b>{escape_html(name)}</b> — {transport}")
        if entry.get("url"):
            lines.append(f"   <code>{escape_html(entry['url'])}</code>")
        if entry.get("command"):
            lines.append(f"   <code>{escape_html(entry['command'])}</code>")
        if not enabled:
            lines.append("   <i>disabled — not attached to sessions</i>")
    lines.append(
        "\n<code>/mcp add|add-sse|add-http|remove|enable|disable|connect|auth|set|env|raw</code>"
    )
    return "\n".join(lines)


async def mcp_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mcp — list, add (stdio/sse/http), remove, enable/disable, connect, auth."""
    settings: Settings = context.bot_data["settings"]
    args = (update.message.text or "").split()[1:]

    if not args:
        await update.message.reply_text(
            _format_mcp_list(settings),
            parse_mode="HTML",
            reply_markup=_mcp_manage_keyboard(
                load_mcp_config(settings).get("mcpServers", {})
            ),
        )
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub == "add" and len(rest) >= 2:
        name, command, *cmd_args = rest
        data = load_mcp_config(settings)
        data.setdefault("mcpServers", {})[name] = {
            "command": command,
            "args": cmd_args,
            "enabled": True,
        }
        save_mcp_config(settings, data)
        _sync_to_claude_cli(name, data["mcpServers"][name], settings)
        await update.message.reply_text(
            f"✅ Added stdio server <b>{escape_html(name)}</b>. Restart to attach.",
            parse_mode="HTML",
        )
        return

    if sub in ("add-sse", "add-http") and len(rest) >= 2:
        name, url = rest[0], rest[1]
        t = "sse" if sub == "add-sse" else "http"
        headers = _parse_headers(rest[2:])
        data = load_mcp_config(settings)
        data.setdefault("mcpServers", {})[name] = {
            "type": t,
            "url": url,
            "enabled": True,
            **({"headers": headers} if headers else {}),
        }
        save_mcp_config(settings, data)
        _sync_to_claude_cli(name, data["mcpServers"][name], settings)
        await update.message.reply_text(
            f"✅ Added {t.upper()} server <b>{escape_html(name)}</b> → "
            f"<code>{escape_html(url)}</code>.\n"
            "<code>/mcp auth " + escape_html(name) + "</code> if it needs OAuth.",
            parse_mode="HTML",
        )
        return

    if sub == "set" and len(rest) >= 2:
        name, raw_json = rest[0], " ".join(rest[1:])
        try:
            entry = json.loads(raw_json)
            if not isinstance(entry, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            await update.message.reply_text(
                f"❌ Invalid JSON: {escape_html(str(e))}", parse_mode="HTML"
            )
            return
        data = load_mcp_config(settings)
        data.setdefault("mcpServers", {})[name] = entry
        save_mcp_config(settings, data)
        await update.message.reply_text(
            f"✅ Updated <b>{escape_html(name)}</b> with custom config. Restart to apply.",
            parse_mode="HTML",
        )
        return

    if sub == "env" and len(rest) >= 2:
        name = rest[0]
        data = load_mcp_config(settings)
        servers = data.setdefault("mcpServers", {})
        if name not in servers:
            await update.message.reply_text(
                f"❌ Server <b>{escape_html(name)}</b> not found.", parse_mode="HTML"
            )
            return
        env = servers[name].setdefault("env", {})
        for kv in rest[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k] = v
        save_mcp_config(settings, data)
        await update.message.reply_text(
            f"✅ Env updated for <b>{escape_html(name)}</b>: "
            + ", ".join(escape_html(k) for k in env),
            parse_mode="HTML",
        )
        return

    if sub == "remove" and rest:
        name = rest[0]
        await update.message.reply_text(
            f"⚠️ Remove MCP server <b>{escape_html(name)}</b>?",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("mcp_remove", name),
        )
        return

    if sub == "enable" and rest:
        await _mcp_set_enabled(update, context, rest[0], True)
        return
    if sub == "disable" and rest:
        await _mcp_set_enabled(update, context, rest[0], False)
        return

    if sub in ("connect", "reconnect") and rest:
        await mcp_connect(update, context, rest[0])
        return

    if sub == "auth" and rest:
        await mcp_auth(update, context, rest[0])
        return

    if sub == "callback" and rest:
        await mcp_finish_login(update, context, " ".join(rest))
        return

    if sub == "raw":
        path = _mcp_config_path(settings)
        content = path.read_text(encoding="utf-8") if path.is_file() else "{}"
        await update.message.reply_text(
            f"📄 <b>MCP config</b> (<code>{escape_html(str(path))}</code>)\n\n"
            f"<pre>{escape_html(_truncate_text(content))}</pre>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "🔌 <b>MCP Management</b>\n\n"
        "<b>Add:</b>\n"
        "<code>/mcp add &lt;name&gt; &lt;command&gt; [args...]</code> — stdio\n"
        "<code>/mcp add-sse &lt;name&gt; &lt;url&gt; [Header: value]</code>\n"
        "<code>/mcp add-http &lt;name&gt; &lt;url&gt; [Header: value]</code> — streamable\n\n"
        "<b>Manage:</b>\n"
        "<code>/mcp enable|disable &lt;name&gt;</code>\n"
        "<code>/mcp connect|reconnect &lt;name&gt;</code> — health check\n"
        "<code>/mcp auth &lt;name&gt;</code> — OAuth (prints login link)\n"
        "<code>/mcp remove &lt;name&gt;</code>\n"
        "<code>/mcp set &lt;name&gt; &lt;json&gt;</code> — full custom config\n"
        "<code>/mcp env &lt;name&gt; KEY=VALUE ...</code>\n"
        "<code>/mcp raw</code> — show config file",
        parse_mode="HTML",
    )


async def _mcp_set_enabled(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, enabled: bool
) -> None:
    settings: Settings = context.bot_data["settings"]
    data = load_mcp_config(settings)
    servers = data.setdefault("mcpServers", {})
    if name not in servers:
        await update.message.reply_text(
            f"❌ Server <b>{escape_html(name)}</b> not found.", parse_mode="HTML"
        )
        return
    servers[name]["enabled"] = enabled
    save_mcp_config(settings, data)
    state = "enabled" if enabled else "disabled"
    await update.message.reply_text(
        f"{'✅' if enabled else '⛔'} <b>{escape_html(name)}</b> {state}. "
        "Restart to apply.",
        parse_mode="HTML",
    )


async def mcp_connect(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    """Health-check an MCP server (CLI get + local probe fallback)."""
    settings: Settings = context.bot_data["settings"]
    entry = _server_entry(settings, name)
    if entry is None:
        await update.message.reply_text(
            f"❌ Server <b>{escape_html(name)}</b> not found in config.",
            parse_mode="HTML",
        )
        return
    # Prefer the CLI's health check (works for servers registered there)
    rc, out = run_claude_cli(["mcp", "get", name], settings, timeout=60)
    if rc == 0 and out.strip():
        await _reply_cli_result(update, rc, out, f"Connect {name}")
        return
    # Fallback local probe for bot-only servers
    t = entry.get("type", "stdio")
    if t in ("http", "sse"):
        import urllib.request

        url = entry.get("url", "")
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"Accept": "application/json, text/event-stream"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                await update.message.reply_text(
                    f"🟢 <b>{escape_html(name)}</b> reachable (HTTP {resp.status}).",
                    parse_mode="HTML",
                )
        except Exception as e:
            await update.message.reply_text(
                f"🔴 <b>{escape_html(name)}</b> not reachable: {escape_html(str(e))}",
                parse_mode="HTML",
            )
        return
    cmd = entry.get("command", "")
    found = shutil.which(cmd) if cmd else None
    if found:
        await update.message.reply_text(
            f"🟢 <b>{escape_html(name)}</b> — stdio command <code>{escape_html(cmd)}</code> found.\n"
            "Restart to connect the server to sessions.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"🔴 <b>{escape_html(name)}</b> — command <code>{escape_html(cmd)}</code> not found on PATH.",
            parse_mode="HTML",
        )


# --- OAuth login flow (claude mcp login --no-browser) ---


def _login_registry(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.bot_data.setdefault("mcp_logins", {})


def start_mcp_login(name: str, settings: Settings, out_path: Path):
    """Start `claude mcp login <name> --no-browser` in the background."""
    claude = _claude_path()
    if not claude:
        return None
    env = os.environ.copy()
    if settings.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(settings.claude_config_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [claude, "mcp", "login", name, "--no-browser"],
        stdout=open(out_path, "wb"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def poll_login_url(out_path: Path, timeout: int = 40) -> Optional[str]:
    """Wait for the authorization URL in the login process output."""
    import re as _re
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for url in _re.findall(r"https?://[^\s\"'<>]+", text):
            if "localhost" not in url and "127.0.0.1" not in url:
                return url
        time.sleep(1)
    return None


async def mcp_auth(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    """Start the OAuth flow: print the login link, then wait for the callback."""
    settings: Settings = context.bot_data["settings"]
    registry = _login_registry(context)
    if name in registry and registry[name]["proc"].poll() is None:
        await update.message.reply_text(
            f"⏳ Login for <b>{escape_html(name)}</b> already in progress.",
            parse_mode="HTML",
        )
        return
    out_path = Path("logs") / f"mcp_login_{name}.log"
    proc = start_mcp_login(name, settings, out_path)
    if proc is None:
        await update.message.reply_text(
            "❌ <code>claude</code> CLI not found — cannot run OAuth login.",
            parse_mode="HTML",
        )
        return
    registry[name] = {"proc": proc, "out": out_path}
    url = await asyncio_get_event_loop().run_in_executor(None, poll_login_url, out_path)
    if not url:
        proc.kill()
        registry.pop(name, None)
        await update.message.reply_text(
            f"❌ Could not obtain an authorization URL for <b>{escape_html(name)}</b>.\n"
            "The server may not support OAuth.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        f"🔑 <b>Authorize {escape_html(name)}</b>\n\n"
        "1. Open this link and grant access:\n"
        f"<code>{escape_html(url)}</code>\n\n"
        "2. After authorizing you'll be redirected to a <code>localhost</code> URL.\n"
        "3. Paste that redirect URL here, or send it as: "
        "<code>/mcp callback &lt;url&gt;</code>\n\n"
        "I'll complete the connection automatically.",
        parse_mode="HTML",
    )


async def mcp_finish_login(
    update: Update, context: ContextTypes.DEFAULT_TYPE, callback_url: str
) -> bool:
    """Feed the redirect URL back to the pending login process."""
    registry = _login_registry(context)
    name, state = None, None
    for n, s in registry.items():
        if s["proc"].poll() is None:
            name, state = n, s
            break
    if name is None:
        return False
    try:
        state["proc"].stdin.write(callback_url.strip() + "\n")
        state["proc"].stdin.flush()
    except Exception:
        pass
    import time

    deadline = time.time() + 15
    while time.time() < deadline and state["proc"].poll() is None:
        time.sleep(1)
    state["proc"].wait(timeout=5)
    rc = state["proc"].returncode
    registry.pop(name, None)
    try:
        out = state["out"].read_text(encoding="utf-8", errors="replace")
    except OSError:
        out = ""
    icon = "✅" if rc == 0 else "❌"
    await update.message.reply_text(
        f"{icon} <b>Auth completed for {escape_html(name)}</b>\n"
        f"<pre>{escape_html(_truncate_text(out[-1500:]))}</pre>",
        parse_mode="HTML",
    )
    return True


async def mcp_button_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle mng:mcp:<action>:<name> buttons from the /mcp listing."""
    settings: Settings = context.bot_data["settings"]
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 4:
        await query.answer("Invalid")
        return
    action = parts[2]
    name = parts[3]
    data = load_mcp_config(settings)
    servers = data.setdefault("mcpServers", {})

    if action == "refresh":
        await query.edit_message_text(
            _format_mcp_list(settings),
            parse_mode="HTML",
            reply_markup=_mcp_manage_keyboard(servers),
        )
        await query.answer()
        return

    if action == "toggle":
        if name not in servers:
            await query.answer("Not found")
            return
        servers[name]["enabled"] = servers[name].get("enabled", True) is not True
        save_mcp_config(settings, data)
        await query.edit_message_text(
            _format_mcp_list(settings),
            parse_mode="HTML",
            reply_markup=_mcp_manage_keyboard(servers),
        )
        await query.answer("Toggled — restart to apply")
        return

    if action == "connect":
        await query.answer()
        fake = MagicMock()
        fake.message = MagicMock()
        fake.message.reply_text = AsyncMock()
        await mcp_connect(fake, context, name)
        await query.edit_message_text(
            _format_mcp_list(settings),
            parse_mode="HTML",
            reply_markup=_mcp_manage_keyboard(servers),
        )
        return

    if action == "auth":
        await query.answer()
        fake = MagicMock()
        fake.message = MagicMock()
        fake.message.reply_text = AsyncMock()
        await mcp_auth(fake, context, name)
        return

    if action == "remove":
        if name in servers:
            del servers[name]
            save_mcp_config(settings, data)
        await query.edit_message_text(
            _format_mcp_list(settings),
            parse_mode="HTML",
            reply_markup=_mcp_manage_keyboard(servers),
        )
        await query.answer(f"Removed {name}")
        return

    await query.answer("Unknown action")


async def mcp_confirm_remove(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Confirm flow for mcp_remove (mng:confirm:mcp_remove:<name>)."""
    settings: Settings = context.bot_data["settings"]
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 4:
        await query.answer("Invalid")
        return
    name = unquote_plus(parts[3])
    data = load_mcp_config(settings)
    data.setdefault("mcpServers", {}).pop(name, None)
    save_mcp_config(settings, data)
    run_claude_cli(["mcp", "remove", name], settings, timeout=30)
    try:
        await query.edit_message_text(
            f"🗑️ Removed MCP server <b>{escape_html(name)}</b>.", parse_mode="HTML"
        )
    except Exception:  # pragma: no cover
        pass
    await query.answer()


def asyncio_get_event_loop():
    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover
        return asyncio.new_event_loop()


# ---------------------------------------------------------------------------
# Workflow commands: /goal, /loop, /clear, /todo, /plan
# ---------------------------------------------------------------------------


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/goal [text|off] — set a session goal that prefixes every prompt."""
    args = (update.message.text or "").split()[1:]
    if not args:
        goal = context.user_data.get("goal")
        if goal:
            await update.message.reply_text(
                f"🎯 <b>Current goal:</b> {escape_html(goal)}\n"
                "Clear with <code>/goal off</code>.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "🎯 No goal set. Use <code>/goal &lt;text&gt;</code> to set one.",
                parse_mode="HTML",
            )
        return
    if args[0].lower() in ("off", "clear", "none"):
        context.user_data.pop("goal", None)
        await update.message.reply_text("🎯 Goal cleared.")
        return
    goal = " ".join(args)
    context.user_data["goal"] = goal
    await update.message.reply_text(
        f"🎯 <b>Goal set.</b> It will prefix every prompt until cleared.\n"
        f"<i>{escape_html(goal)}</i>",
        parse_mode="HTML",
    )


async def loop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loop [N|off] — auto-continue the session N times after each reply."""
    args = (update.message.text or "").split()[1:]
    if args and args[0].lower() in ("off", "stop", "0"):
        context.user_data.pop("loop_remaining", None)
        await update.message.reply_text("🔁 Loop stopped.")
        return
    n = 3
    if args:
        try:
            n = max(1, min(int(args[0]), 10))
        except ValueError:
            await update.message.reply_text(
                "🔁 Usage: <code>/loop [N]</code> (1-10) or <code>/loop off</code>.",
                parse_mode="HTML",
            )
            return
    context.user_data["loop_remaining"] = n
    await update.message.reply_text(
        f"🔁 Loop mode: will auto-continue up to <b>{n}</b> time(s) per message. "
        "Stop with <code>/loop off</code>.",
        parse_mode="HTML",
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear — clear session context (same as /new)."""
    context.user_data["claude_session_id"] = None
    context.user_data["force_new_session"] = True
    context.user_data.pop("goal", None)
    context.user_data.pop("loop_remaining", None)
    await update.message.reply_text(
        "🧹 Context cleared. Next message starts a fresh session."
    )


async def todo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/todo [add <item> | done <n> | clear] — per-session todo list."""
    args = (update.message.text or "").split()[1:]
    todos: List[str] = context.user_data.setdefault("todos", [])

    if args and args[0].lower() == "add" and len(args) > 1:
        item = " ".join(args[1:])
        todos.append(item)
        await update.message.reply_text(
            f"✅ Added todo <b>{len(todos)}</b>: {escape_html(item)}",
            parse_mode="HTML",
        )
        return
    if args and args[0].lower() == "done" and len(args) > 1:
        try:
            idx = int(args[1]) - 1
            removed = todos.pop(idx)
            await update.message.reply_text(
                f"✅ Done: <s>{escape_html(removed)}</s>", parse_mode="HTML"
            )
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid todo number.")
        return
    if args and args[0].lower() == "clear":
        context.user_data["todos"] = []
        await update.message.reply_text("🗑️ Todo list cleared.")
        return

    if not todos:
        await update.message.reply_text(
            "📋 No todos. Use <code>/todo add &lt;item&gt;</code>.",
            parse_mode="HTML",
        )
        return
    lines = ["📋 <b>Todos</b>"]
    lines += [f"{i}. {escape_html(t)}" for i, t in enumerate(todos, 1)]
    lines.append(
        "\n<code>/todo add &lt;item&gt;</code> · <code>/todo done &lt;n&gt;</code> · <code>/todo clear</code>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan [off] — set permission mode to plan (alias of /permission plan)."""
    args = (update.message.text or "").split()[1:]
    if args and args[0].lower() in ("off", "reset"):
        context.user_data.pop("permission_mode", None)
        await update.message.reply_text(
            "📋 Permission mode cleared — using the backend default."
        )
        return
    context.user_data["permission_mode"] = "plan"
    await update.message.reply_text(
        "📋 <b>Plan mode enabled.</b> Claude will propose changes without "
        "applying them.\nExit with <code>/plan off</code> or <code>/permission reset</code>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Read-only commands: /memory, /hooks, /config
# ---------------------------------------------------------------------------


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/memory — show Claude Code's global memory (CLAUDE.md)."""
    settings: Settings = context.bot_data["settings"]
    candidates = []
    if settings.claude_config_dir:
        candidates.append(settings.claude_config_dir / "CLAUDE.md")
    candidates.append(Path.home() / ".claude" / "CLAUDE.md")
    for path in candidates:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            await update.message.reply_text(
                f"🧠 <b>Memory</b> (<code>{escape_html(str(path))}</code>)\n\n"
                f"<pre>{escape_html(_truncate_text(content))}</pre>",
                parse_mode="HTML",
            )
            return
    await update.message.reply_text(
        "🧠 No <code>CLAUDE.md</code> memory file found.", parse_mode="HTML"
    )


async def hooks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hooks — list hooks configured in Claude Code settings.json."""
    settings: Settings = context.bot_data["settings"]
    if not settings.claude_config_dir:
        await update.message.reply_text(
            "ℹ️ <code>CLAUDE_CONFIG_DIR</code> not set — cannot locate settings.json.",
            parse_mode="HTML",
        )
        return
    settings_path = settings.claude_config_dir / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        await update.message.reply_text(
            "❌ Could not read settings.json.", parse_mode="HTML"
        )
        return
    hooks = data.get("hooks") or {}
    if not hooks:
        await update.message.reply_text("🔧 No hooks configured.")
        return
    lines = ["🔧 <b>Hooks</b>"]
    for event, entries in hooks.items():
        for entry in entries or []:
            for h in entry.get("hooks") or []:
                matcher = entry.get("matcher") or "*"
                cmd = (h.get("command") or "").replace("\n", " ")[:120]
                lines.append(
                    f"• <b>{escape_html(event)}</b> [{escape_html(matcher)}]\n  <code>{escape_html(cmd)}</code>"
                )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/config — show the bot's effective configuration."""
    settings: Settings = context.bot_data["settings"]
    from ...config.features import FeatureFlags

    flags = FeatureFlags(settings)
    enabled = sorted(flags.get_enabled_features())
    backend = context.user_data.get("backend") or "sdk"
    lines = [
        "⚙️ <b>Effective Configuration</b>",
        f"• Mode: <code>{'agentic' if settings.agentic_mode else 'classic'}</code>",
        f"• Backend: <code>{backend}</code>",
        f"• Model: <code>{settings.claude_model or 'default'}</code>",
        f"• Base URL: <code>{settings.anthropic_base_url or 'default'}</code>",
        f"• Approved dir: <code>{escape_html(str(settings.approved_directory))}</code>",
        f"• Claude config dir: <code>{escape_html(str(settings.claude_config_dir)) if settings.claude_config_dir else '—'}</code>",
        f"• Scheduler: <code>{'on' if settings.enable_scheduler else 'off'}</code>",
        f"• MCP: <code>{'on' if settings.enable_mcp else 'off'}</code>",
        f"• Remote approvals: <code>{'on' if settings.enable_remote_approvals else 'off'}</code>",
        f"• Features: {', '.join(f'<code>{f}</code>' for f in enabled) or '—'}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /compact and /review — lightweight runs via the integration facade
# ---------------------------------------------------------------------------


async def _simple_run(
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    force_new: bool = False,
    session_id: Optional[str] = None,
) -> str:
    """Run one prompt through the active backend; return the text reply."""
    import asyncio

    integration = context.bot_data.get("claude_integration")
    if not integration:
        return "Claude integration not available."
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    resp = await integration.run_command(
        prompt=prompt,
        working_directory=Path(current_dir),
        user_id=0,
        session_id=session_id,
        force_new=force_new,
        interrupt_event=asyncio.Event(),
    )
    if force_new:
        context.user_data["claude_session_id"] = resp.session_id
    return resp.content or ""


async def compact_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/compact — summarize the conversation and continue fresh with the summary."""
    session_id = context.user_data.get("claude_session_id")
    if not session_id:
        await update.message.reply_text(
            "💾 No active session to compact. Start one with <code>/new</code>.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text("💾 Compacting conversation…")
    summary = await _simple_run(
        context,
        "Produce a concise bullet-point summary of our entire conversation so far. "
        "Include goals, decisions, files touched, and open items. Output ONLY the summary.",
        session_id=session_id,
    )
    if not summary:
        await update.message.reply_text("❌ Compaction produced no summary.")
        return
    context.user_data["claude_session_id"] = None
    context.user_data["force_new_session"] = True
    await _simple_run(
        context,
        f"This is a summary of our previous conversation:\n{summary}\n\n"
        "Continue from here. Acknowledge briefly.",
        force_new=True,
    )
    await update.message.reply_text(
        "💾 <b>Context compacted.</b> New session seeded with the summary.",
        parse_mode="HTML",
    )


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/review — ask Claude to review the current git diff."""
    import asyncio

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    git_dir = subprocess.run(
        ["git", "-C", str(current_dir), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=flags,
    )
    if git_dir.returncode != 0:
        await update.message.reply_text(
            f"❌ <code>{escape_html(str(current_dir))}</code> is not a git repository.",
            parse_mode="HTML",
        )
        return
    diff = subprocess.run(
        ["git", "-C", str(current_dir), "diff", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=flags,
    )
    status = subprocess.run(
        ["git", "-C", str(current_dir), "status", "--short"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=flags,
    )
    diff_text = (diff.stdout or "(no diff)")[:8000]
    status_text = (status.stdout or "").strip() or "(clean)"
    prompt = (
        "Review the current changes in this repository.\n\n"
        f"Changed files:\n{status_text}\n\nDiff:\n{diff_text}\n\n"
        "Give a concise code review: bugs, risks, style, and suggestions."
    )
    await update.message.reply_text("🔍 Reviewing changes…")
    review = await _simple_run(context, prompt)
    await update.message.reply_text(
        f"🔍 <b>Code Review</b>\n\n{_truncate_text(review)}",
        parse_mode="HTML",
    )
    _ = asyncio  # keep import used on platforms without git
