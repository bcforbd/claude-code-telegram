# Plan — Cowork + Claude Desktop (developer mode) support for claude-code-telegram

**Date:** 2026-08-14 · **Status:** ✅ Implemented, tested, and verified (600 tests passing)
**Research inputs:** `.research/codebase-analysis.md`, `.research/cowork-research.md`, `.research/claude-desktop-research.md`

---

## 1. Key research findings (what we learned)

1. **Claude Cowork is not a separate engine.** It is a consumer GUI mode of the same agent architecture that powers Claude Code. There is **no Cowork API/CLI**. The programmatic surface is the Claude Code engine via the **Agent SDK / CLI** — which this bot already drives via `claude-agent-sdk`.
2. **Claude Desktop "developer mode" exposes NO inbound HTTP API.** The real, useful parts of developer mode are: (a) the `claude_desktop_config.json` file (`mcpServers` + `env` block incl. `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` — outbound gateway routing), and (b) MCP servers the Desktop app runs. Claude Code can **import Desktop's MCP config**.
3. **Conclusion:** "Support Cowork" and "Support Claude Desktop (developer mode)" = add **backend modes** that configure the same engine the way Cowork / Desktop do:
   - **desktop** backend → load MCP servers + env (incl. dev-mode gateway routing) from the real `claude_desktop_config.json` on this machine (`%APPDATA%\Claude\claude_desktop_config.json`).
   - **cowork** backend → Cowork-style profile: account conversation preferences as system prompt (this machine: `cowork_account_settings.json`), Cowork default model (`auto/claude-opus`), permission mode `acceptEdits` (≈ Cowork Auto), web search on, optional Cowork plugin dirs.
4. **Remote approvals are the missing "remote control" piece** (pattern proven by `claude-remote-TGbot`): route SDK `can_use_tool` permission requests to Telegram inline **Approve / Deny / Always-allow** buttons and wait for the answer. This makes Desktop/Cowork usable fully from Telegram.

---

## 2. Architecture: backend abstraction + router

```
Telegram ──► Orchestrator ──► ClaudeIntegration (facade, unchanged interface)
                                  │  run_command() [session mgmt, resume, usage]
                                  ▼
                          BackendRouter.select(chat_override, config default)
                                  │
              ┌───────────────────┼──────────────────────┐
              ▼                   ▼                      ▼
      ClaudeSDKManager      ClaudeDesktopManager    CoworkManager
      (existing)            (new)                   (new)
      = "sdk" backend       = "desktop" backend     = "cowork" backend
```

- `ClaudeBackend` Protocol: `execute_command(prompt, working_directory, session_id, continue_session, stream_callback, interrupt_event, images) -> ClaudeResponse` (+ `name`, `describe()` for `/status`).
- All backends emit `StreamUpdate` and return `ClaudeResponse` (existing wire types).
- Per-chat override stored in `context.user_data["backend"]`; default from `Settings.claude_backend`.
- Session ids: backends return the real Claude session id in `ClaudeResponse.session_id` → auto-resume keeps working.

## 3. New settings (`.env`)

| Env var | Default | Purpose |
|---|---|---|
| `CLAUDE_BACKEND` | `sdk` | `sdk` \| `desktop` \| `cowork` (default backend) |
| `DESKTOP_CONFIG_PATH` | `%APPDATA%/Claude/claude_desktop_config.json` | Claude Desktop config to import (mcpServers + env) |
| `DESKTOP_APP_CONFIG_PATH` | (auto) | Claude-3p desktop config discovery on this machine |
| `COWORK_SETTINGS_PATH` | (auto) | `cowork_account_settings.json` for preferences/model |
| `COWORK_MODEL` | `auto/claude-opus` | Model used by cowork backend |
| `COWORK_PERMISSION_MODE` | `acceptEdits` | SDK permission mode for cowork backend |
| `COWORK_ENABLE_WEB_SEARCH` | `true` | Add WebSearch tool (matches `coworkWebSearchEnabled`) |
| `COWORK_PLUGIN_DIRS` | (empty) | Comma-separated plugin dirs to load (e.g. 3p marketplace) |
| `ENABLE_REMOTE_APPROVALS` | `false` | Route tool permission requests to Telegram inline buttons |
| `APPROVAL_TIMEOUT_SECONDS` | `120` | How long the bot waits for an approve/deny decision |
| `DEFAULT_PERMISSION_MODE` | (unset) | Optional SDK permission mode override for sdk/desktop backends |

## 4. New files

| File | Contents |
|---|---|
| `src/claude/backend.py` | `BackendName` Literal, `ClaudeBackend` Protocol, `BackendRouter` (select by chat override + config), `backend_status()` helper |
| `src/claude/desktop_integration.py` | `ClaudeDesktopManager`: locate/parse `claude_desktop_config.json`; build MCP servers + env overlay (dev-mode gateway routing); delegate execution to `ClaudeSDKManager` with those options; `status()` (config found, MCP servers, env keys, app running) |
| `src/claude/cowork_integration.py` | `CoworkManager`: load cowork account settings (conversation_preferences → system prompt, model, web search), optional plugin dirs; delegate to `ClaudeSDKManager`; `status()` |
| `src/bot/features/approvals.py` | `ApprovalBridge`: send inline keyboard (Approve/Deny/Always), wait via `asyncio.Event` keyed by request id, expire on timeout; `parse_approval_callback()` |
| `src/bot/handlers/approval.py` | `approval_callback_handler` for `approve:` prefix (agentic + classic) |

## 5. Modified files

| File | Change |
|---|---|
| `src/config/settings.py` | New fields above + validators |
| `src/claude/sdk_integration.py` | Optional `can_use_tool` override hook → `ApprovalBridge` when remote approvals enabled; export `build_mcp_servers_from_desktop_config()` helper (shared) |
| `src/claude/facade.py` | Accept `backends: Dict[str, ClaudeBackend]`; `_execute()` → `self.router.select(...)`; `backend` param threaded from handlers; `get_backend_status()` |
| `src/main.py` | Build all 3 backends + router; inject approval bridge; pass to facade |
| `src/bot/orchestrator.py` | New agentic commands `/backend /desktop /cowork /model /permission /mcp`; register `approve:` callback; include backend name in `/status`; `_make_stream_callback` extended for new MCP tools |
| `src/bot/handlers/command.py` | Classic-mode versions of the new commands |
| `src/bot/handlers/callback.py` | Delegate `approve:` callbacks |
| `src/mcp/telegram_server.py` | New tools: `send_document_to_user`, `notify_user` (pure validation; delivery via stream interception) |
| `.env.example` | Document new vars |
| `docs/configuration.md`, `docs/backend-modes.md` | Docs |
| `README.md`, `CHANGELOG.md` | Feature list + version note |

## 6. New commands

| Command | Behavior |
|---|---|
| `/backend [sdk\|desktop\|cowork]` | Show/set per-chat backend |
| `/desktop` | Desktop config status: app config path, MCP servers loaded, dev-mode env (base URL), app running; `ON` = desktop backend active |
| `/cowork` | Cowork profile status: model, permission mode, web search, plugins |
| `/model [name]` | Show/set per-chat model override |
| `/permission [mode]` | Show/set SDK permission mode (acceptEdits/plan/bypassPermissions/dontAsk/manual) |
| `/mcp` | List MCP servers active for current backend |

## 7. Remote approval flow (ENABLE_REMOTE_APPROVALS=true)

1. SDK calls `can_use_tool(tool_name, tool_input, context)`.
2. Bot sends `🛡️ <tool> wants to run …` + buttons `✅ Approve` `⛔ Deny` `♻️ Always allow`.
3. Bot waits on request-id `asyncio.Event` (timeout = APPROVAL_TIMEOUT_SECONDS → default deny).
4. Callback handler resolves the event → `PermissionResultAllow()/Deny()` (Always-allow caches tool name for the session).
5. Decision injected back into SDK → Claude continues; result streamed as usual.

## 8. Test plan

- `tests/unit/test_claude/test_desktop_integration.py` — config parse (found/missing/invalid), MCP servers + env mapping, `status()`.
- `tests/unit/test_claude/test_cowork_integration.py` — settings load, preferences → system prompt, model/permission defaults, plugin dirs.
- `tests/unit/test_claude/test_backend.py` — router selection (default, per-user override, invalid), facade dispatch with mock backends.
- `tests/unit/test_claude/test_approvals.py` — bridge allow/deny/always/timeout.
- `tests/unit/test_bot/` — new command handlers (agentic + classic), `approve:` callback parsing.
- `tests/unit/test_mcp/` — new MCP tools validations.
- Full suite: `pytest` (baseline 530 tests must stay green) + `mypy` + `flake8` + `black --check`.

## 9. Verification steps

1. `pytest` full suite green (530 + new).
2. `mypy src` / `flake8` / `black --check` clean.
3. Runtime smoke: `python -c` instantiate `Settings` + `ClaudeDesktopManager.status()` against the real `%APPDATA%\Claude\claude_desktop_config.json` and the Claude-3p cowork settings; assert desktop MCP servers (desktop-commander, telegram) are parsed; assert cowork preferences load.
4. Manual (user): run bot with `CLAUDE_BACKEND=desktop`, send message → response via Desktop's MCP-enabled agent; `/backend cowork` → Cowork profile.

## 10. Sequencing

1. Settings → 2. backend.py + router → 3. desktop_integration.py → 4. cowork_integration.py → 5. approvals.py → 6. facade/main wiring → 7. commands (agentic + classic) → 8. MCP tools → 9. tests → 10. docs/.env → 11. run suite + lint → 12. runtime smoke against real configs → 13. user verification checklist.
