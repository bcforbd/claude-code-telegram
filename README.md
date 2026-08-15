# Claude Code Telegram Bot

[![CI](https://github.com/bcforbd/claude-code-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/bcforbd/claude-code-telegram/actions/workflows/ci.yml)
[![Release](https://github.com/bcforbd/claude-code-telegram/actions/workflows/release.yml/badge.svg)](https://github.com/bcforbd/claude-code-telegram/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Drive Claude Code, Claude Desktop, and Claude Cowork from Telegram — securely, from anywhere.**

A self-hosted Telegram bot that gives you a terminal-like interface to the Claude agent
engine. Send a message, get a reply. Upload files, review diffs, approve tool calls,
and manage long-running sessions — all from your phone.

> **Fork notice:** This is a maintained fork of the original
> [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) project by
> Richard Atkinson, extended with backend modes (Desktop/Cowork), remote approvals,
> insight & control commands, and more. See [License & Attribution](#license--attribution).

---

## Table of Contents

- [Features](#features)
- [Backends](#backends)
- [Commands](#commands)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Security](#security)
- [Deployment](#deployment)
- [Development](#development)
- [Project Structure](#project-structure)
- [Documentation](#documentation)
- [License & Attribution](#license--attribution)

---

## Features

### Core

- **Remote Claude sessions** — start, continue, interrupt, and end conversations from Telegram
- **Three backend profiles** — run the engine the way Claude Code, Claude Desktop, or
  Claude Cowork configures it (see [Backends](#backends))
- **Remote approvals** — route Claude tool permission requests to Telegram inline
  **Approve / Deny / Always allow** buttons; default-deny on timeout
- **Per-chat settings** — backend, model, and permission-mode overrides that survive restarts

### Input & output

- **File uploads** — send files or archives (zip/tar/gz), extracted and previewed automatically
- **Image analysis** — screenshots and photos analyzed in-context
- **Voice messages** — transcription via Mistral Voxtral, OpenAI Whisper, or local whisper.cpp
- **Session export** — download conversation history in multiple formats

### Productivity

- **Git integration** — safe, read-only git operations with a command allowlist
- **Quick actions** — context-aware action buttons for common tasks
- **Follow-up suggestions** — smart conversation-mode prompts
- **Project thread mode** — route work by Telegram forum topics (private chat or supergroup)
- **Job scheduler** — schedule recurring bot/agent tasks
- **MCP server** — expose the bot as a Model Context Protocol server

### Control & observability

- **Insight commands** — `/cost` (real SDK spend + budget), `/stats`, `/sessions`,
  `/context`, `/jobs`, `/doctor`
- **Rate limiting** — per-window request limits with burst capacity
- **Cost caps** — hard per-request and per-user lifetime budgets
- **Audit logging** — structured JSON logs of security-relevant events

---

## Backends

The bot drives the Claude agent engine (via `claude-agent-sdk`) in three profiles:

| Backend | Name | What it mirrors |
|---|---|---|
| `sdk` | Claude Code | Plain Claude Code — the original behavior |
| `desktop` | Claude Desktop | Imports `claude_desktop_config.json` (MCP servers + env, incl. dev-mode gateway routing) |
| `cowork` | Claude Cowork | Cowork account preferences as system prompt, `auto/claude-opus` model, `acceptEdits` permissions, web search, plugins |

Select globally with `CLAUDE_BACKEND` in `.env`, or per chat with `/backend`.
See [docs/backend-modes.md](docs/backend-modes.md) for the full design.

---

## Commands

### Sessions

| Command | Description |
|---|---|
| `/start` | Begin a new conversation |
| `/new` | Start a fresh session (alias) |
| `/continue` | Resume the active session |
| `/end` | End the current session |
| `/stop` | Interrupt the running request |
| `/loop` | Toggle agentic loop mode |
| `/compact` | Compact the conversation context |
| `/clear` | Clear conversation state |

### Files & projects

| Command | Description |
|---|---|
| `/ls` | List files in the working directory |
| `/cd <path>` | Change the working directory |
| `/pwd` | Show the current working directory |
| `/projects` | List / switch project roots |
| `/git <cmd>` | Safe read-only git operations |
| `/export` | Export the session in another format |

### Insights & control

| Command | Description |
|---|---|
| `/cost` | Real SDK spend and remaining budget |
| `/stats` | Usage dashboard |
| `/sessions` | List sessions; switch or resume inline |
| `/context` | Inspect the current session context |
| `/jobs` | Scheduled-job management (list / add / remove) |
| `/doctor` | Self-diagnostics checklist |
| `/status` | Bot status |

### Backends & models

| Command | Description |
|---|---|
| `/backend [sdk\|desktop\|cowork]` | Show or switch the engine profile |
| `/model [name]` | Show or set the model |
| `/permission [mode]` | Show or set the permission mode |
| `/desktop` | Switch to the Desktop backend (shortcut) |
| `/cowork` | Switch to the Cowork backend (shortcut) |

### Configuration & management

| Command | Description |
|---|---|
| `/config` | View or edit runtime configuration |
| `/features` | List feature flags |
| `/plugins` / `/skills` | Manage plugins and skills |
| `/actions` | List available quick actions |
| `/mcp` | Model Context Protocol status / management |
| `/hooks` | Manage hooks |
| `/memory` | View agent memory |
| `/todo` | View / manage todo list |
| `/review` | Trigger a code review |
| `/plan` | Enter plan mode |
| `/goal` | Goal management |
| `/verbose` | Toggle verbose output |
| `/repo` | Show repository info |
| `/restart` | Restart the bot |
| `/help` | Show help |

---

## Quick Start

### Prerequisites

- **Python 3.11+** ([download](https://www.python.org/downloads/))
- **Poetry** (or `uv`) for dependency management
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- **Claude authentication** — one of:
  - Claude CLI logged in (`claude auth login`) — recommended
  - An `ANTHROPIC_API_KEY`

### Install & run

```bash
git clone https://github.com/bcforbd/claude-code-telegram.git
cd claude-code-telegram

# Poetry
poetry install --with dev
poetry run claude-telegram-bot

# or uv
uv sync
uv run claude-telegram-bot
```

### Configure

```bash
cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=<token from BotFather>
#   TELEGRAM_BOT_USERNAME=<bot username>
#   APPROVED_DIRECTORY=/path/to/your/projects
#   ALLOWED_USERS=<your Telegram numeric user id>
```

Message your bot on Telegram: `/start` to begin, `/help` for commands.

> **Windows users:** the repo includes [scripts/bot-watcher.ps1](scripts/bot-watcher.ps1),
> an optional helper that starts the bot automatically with Claude Desktop and stops it
> when Claude Desktop closes.

---

## Configuration

All configuration is environment-variable driven (`.env`). Key settings:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (**required**) |
| `APPROVED_DIRECTORY` | Root path the bot may access (**required**) |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to use the bot |
| `ENABLE_TOKEN_AUTH` | Require a per-user auth token |
| `AUTH_TOKEN_SECRET` | Secret for generating auth tokens |
| `USE_SDK` | Use the Claude Python SDK (`true`) or CLI subprocess (`false`) |
| `ANTHROPIC_API_KEY` | API key (optional if Claude CLI is authenticated) |
| `CLAUDE_BACKEND` | Default backend: `sdk`, `desktop`, or `cowork` |
| `CLAUDE_MAX_COST_PER_USER` | Lifetime USD budget per user |
| `CLAUDE_MAX_COST_PER_REQUEST` | Hard USD cap per request |
| `RATE_LIMIT_REQUESTS` | Requests allowed per window |
| `ENABLE_REMOTE_APPROVALS` | Route tool approvals to Telegram buttons |
| `ENABLE_PROJECT_THREADS` | Forum-topic project routing |

See [docs/configuration.md](docs/configuration.md) for the full reference and
[.env.example](.env.example) for every option with comments.

---

## Security

The bot is designed to be exposed to the internet, so security is layered:

- **User whitelist** — only Telegram user IDs in `ALLOWED_USERS` may interact
  (empty = allow all; **not recommended**)
- **Token auth** — optional per-user bearer tokens via `ENABLE_TOKEN_AUTH`
- **Path & command validation** — dangerous patterns (pipes, redirection, injection)
  are rejected before reaching the shell
- **Tool allowlist** — Claude tools are restricted to a curated set;
  `CLAUDE_ALLOWED_TOOLS` overrides
- **Rate limiting** — per-window request caps with burst control
- **Cost caps** — per-request and per-user lifetime budgets enforced at the SDK level
- **Remote approvals default-deny** — permission requests time out to denial
- **Audit logging** — structured, security-relevant event log
- **Approved root** — file access confined to `APPROVED_DIRECTORY`

See [SECURITY.md](SECURITY.md) for the security policy and architecture, and
[CONTRIBUTING.md](CONTRIBUTING.md) for reporting a vulnerability.

> **Self-hosting note:** the real secrets live in your local `.env`, which is
> gitignored. Never commit `.env`, `logs/`, or `data/` (the SQLite database
> contains conversation history).

---

## Deployment

- **Linux / systemd:** see [SYSTEMD_SETUP.md](SYSTEMD_SETUP.md) for a persistent
  user service (auto-restart, logs, hardening).
- **Windows:** see `scripts/bot-watcher.ps1` for tying the bot's lifecycle to
  Claude Desktop (or run `python -m src.main` from the project root).

---

## Development

```bash
# Install dev dependencies
poetry install --with dev

# Run tests (unit + integration)
make test          # or: poetry run pytest

# Lint & format checks
make lint          # black, isort, flake8, mypy

# Auto-fix formatting
make format

# Run with auto-reload on code changes
make run-watch
```

**Code standards:** Python 3.11+, Black formatting (88 cols), type hints required,
pytest with >85% coverage. Pre-commit hooks are configured — `poetry run pre-commit install`.

CI (GitHub Actions) runs formatting, lint, tests, and secret scanning
on every push/PR to `main`. Releases are built automatically from `v*` tags
(see [.github/workflows](.github/workflows)).

---

## Project Structure

```
src/
├── main.py                 # Entry point
├── api/                    # Local HTTP API
├── bot/                    # Telegram bot core, handlers, features, middleware
├── claude/                 # Engine integration: facade, SDK, backends, monitor
├── config/                 # Pydantic settings, environment loading, feature flags
├── events/                 # Event bus
├── mcp/                    # Model Context Protocol server
├── notifications/          # Notification service
├── projects/               # Project registry + thread manager
├── scheduler/              # Job scheduler
├── security/               # Auth, validation, rate limiting, audit
├── storage/                # SQLite persistence (facade, repositories)
└── utils/                  # Shared helpers
```

---

## Documentation

| Doc | Description |
|---|---|
| [Setup & Installation](docs/setup.md) | Prerequisites, authentication, install methods |
| [Configuration](docs/configuration.md) | Full environment variable reference |
| [Backend Modes](docs/backend-modes.md) | sdk / desktop / cowork profiles design |
| [Available Tools](docs/tools.md) | Claude tool allowlist & monitor behaviour |
| [Development](docs/development.md) | Dev setup, testing, contribution workflow |
| [Project Overview](docs/project-overview.md) | Architecture & design decisions |

---

## License & Attribution

MIT License — see [LICENSE](LICENSE).

This project is a fork of [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram)
by Richard Atkinson (MIT). New work: backend modes (Desktop/Cowork), remote approvals,
insight & control commands, and additional hardening. Third-party components remain
under their respective licenses.
