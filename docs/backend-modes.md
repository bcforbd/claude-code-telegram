# Backend Modes: Claude Code / Claude Desktop / Claude Cowork

The bot can drive the Claude Code engine in three profiles ("backends").
All backends share the same agent loop (via `claude-agent-sdk`) — they
differ in **which configuration the engine runs with**.

| Backend | Name | What it mirrors |
|---|---|---|
| `sdk` | Claude Code (SDK) | Plain Claude Code — the original behavior |
| `desktop` | Claude Desktop mirror | The Claude Desktop app: imports `claude_desktop_config.json` (MCP servers + env, incl. developer-mode gateway routing) |
| `cowork` | Claude Cowork mirror | Claude Cowork: Cowork model default, account conversation preferences as system prompt, `acceptEdits` permissions, web search, plugins |

## Why this design

Research (`.research/`) established two important facts:

1. **Claude Cowork has no separate API.** It is a consumer GUI mode of the
   same agent architecture that powers Claude Code. The programmatic surface
   is the Claude Code engine via the Agent SDK / CLI.
2. **Claude Desktop "developer mode" exposes no inbound HTTP API.** The real,
   useful parts of developer mode are the Desktop config file
   (`claude_desktop_config.json`: `mcpServers` + `env`) — which Claude Code
   can import and run with.

So "support Cowork" / "support Claude Desktop (developer mode)" = run the
engine with those profiles.

## Selecting a backend

- **Global default:** `CLAUDE_BACKEND=sdk|desktop|cowork` in `.env`
- **Per chat:** `/backend desktop` (or `/backend sdk|cowork`) — the choice is
  stored per chat and survives restarts (kept in `user_data`).

Switching backend resets the Claude session for that chat.

## Desktop backend (`desktop`)

- Reads `%APPDATA%/Claude/claude_desktop_config.json` (override with
  `DESKTOP_CONFIG_PATH`).
- Loads every valid `mcpServers` entry (e.g. `desktop-commander`,
  `telegram`) into the agent.
- Applies the `env` block — including developer-mode gateway routing
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, ...).
- `/desktop` shows: config found, MCP servers, gateway, dev-mode env.

## Cowork backend (`cowork`)

- Auto-discovers `cowork_account_settings.json` from the local Claude
  Desktop install (override with `COWORK_SETTINGS_PATH`); prefers the
  profile with real content (`__account_profile`,
  `__model_selector_state`, ...).
- **System prompt:** loads `conversation_preferences`
  (top-level or inside `__account_profile`) into the prompt.
- **Model:** `COWORK_MODEL` → account model selector
  (`__model_selector_state.cowork.model`) → `auto/claude-opus`.
- **Permissions:** `COWORK_PERMISSION_MODE` (default `acceptEdits`, ≈
  Cowork's Auto approval).
- **Web search:** `COWORK_ENABLE_WEB_SEARCH` (default true) adds the
  `WebSearch` tool.
- **Plugins:** `COWORK_PLUGIN_DIRS` (comma-separated paths) loads local
  plugins.
- `/cowork` shows: settings found, model, permission mode, web search,
  plugins, preferences loaded.

## Remote approvals (drive the agent from Telegram)

`ENABLE_REMOTE_APPROVALS=true` routes Claude tool permission requests to
Telegram inline buttons:

```
🛡️ Bash wants to run:
{"command": "npm install"}

[✅ Approve] [⛔ Deny]
[♻️ Always allow]
```

- Security validation (path/boundary checks) always runs first.
- Only tools in the ask-set (Bash, Write, Edit, WebFetch, WebSearch, Task,
  Skill, ...) prompt; read-only tools stay auto-allowed.
- `APPROVAL_TIMEOUT_SECONDS` (default 120) — timeout = deny.
- `♻️ Always allow` caches the tool for the session.

## Per-chat overrides

| Command | Effect |
|---|---|
| `/backend [sdk\|desktop\|cowork]` | Show / switch backend |
| `/model [name\|reset]` | Per-chat model override |
| `/permission [mode\|reset]` | Per-chat permission mode (`acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `manual`) |
| `/desktop` | Desktop config status |
| `/cowork` | Cowork profile status |
| `/mcp` | List MCP servers active for the current backend |

## New MCP tools (telegram server)

- `send_image_to_user(file_path, caption)` — existing
- `send_document_to_user(file_path, caption)` — send PDF/DOCX/XLSX/ZIP/code
- `notify_user(message)` — push a notification to the chat

## Architecture

```
Telegram ──► MessageOrchestrator ──► ClaudeIntegration (facade)
                                          │  run_command(..., backend=...)
                                          ▼
                                  BackendRouter.select(chat_override, config)
                                          │
                    ┌─────────────────────┼──────────────────────┐
                    ▼                     ▼                      ▼
              SdkBackend           ClaudeDesktopManager    CoworkManager
              (sdk)                (desktop)               (cowork)
                    └──────────────┬──────────────┘
                                  ▼
                          ClaudeSDKManager
                     (claude-agent-sdk, shared)
```

New files: `src/claude/backend.py` (protocol + router),
`src/claude/desktop_integration.py`, `src/claude/cowork_integration.py`,
`src/bot/features/approvals.py` (bridge), `src/bot/handlers/approval.py`.
