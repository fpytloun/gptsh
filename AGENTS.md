# GPTSH AGENTS DEVELOPMENT GUIDE

This document describes the requirements, architecture, and development instructions for AI agents building and maintaining the `gptsh` shell client.

---

## How to use this document

- Agent-facing guidance: actionable rules and checklists for LLM agents are in the "AGENT-FACING GUIDANCE" section near the end. Start there if you are an LLM building or maintaining code.
- Project reference: architecture, configuration, tooling, and CLI semantics are under "PROJECT REFERENCE". Use these sections as the authoritative source of facts about the project.
- Safety: never execute destructive actions without explicit approval. See Approval UX and the Agent Safety Rules for constraints.

## Section map

- AGENT-FACING GUIDANCE
  - Development Instructions
  - Safety Rules (approvals, secrets, execution constraints)
  - Memory and Planning
  - Task Completion Checklist
- PROJECT REFERENCE
  - Project Vision, Tech Stack, Structure
  - Configuration, MCP, Execution Model, Logging
  - Testing, Packaging, CLI Usage, Exit Codes

---

## PROJECT REFERENCE

### Project Vision

A modular, extensible, and secure Python shell client that empowers developers and system administrators with conversational and tool-augmented AI using the latest GPT-like LLMs, based on [litellm](https://docs.litellm.ai/). It natively supports the Model Context Protocol (MCP) for tool integrations (including Claude and others)

---
### Tech Stack
 - **Python 3.10+**, fully typed
- **litellm** for LLM APIs (OpenAI, Claude, Perplexity, Azure, etc.)
- **mcp** Python SDK for Model Context Protocol (MCP) ([documentation](https://openai.github.io/openai-agents-python/mcp/))
- **pyyaml** for config loading/merging
- **click** for interactive CLI
- **rich** for progress UI and Markdown rendering
- **textual** (future, optional) for TUI
- **asyncio**, **httpx** for async operations, especially for MCP/http/SSE
- **uv/uvx** as the only accepted way to install/manage dependencies & run dev scripts
- **pytest** for testing
- **python-dotenv/os** for environment variable expansion in configs

---
### Project Structure
```
gptsh/
  cli/
    entrypoint.py        # CLI entry, args, REPL handoff
    repl.py              # REPL commands and loop (Agent-based)
    utils.py             # CLI utilities (agent resolution, listings)
  config/
    loader.py            # config loading, env expansion, !include support
  core/
    approval.py          # DefaultApprovalPolicy (wildcards + TTY-aware confirm)
    config_api.py        # helpers to resolve agent/provider, outputs, tools policy (uses core.models)
    agent.py             # Agent dataclass and ToolHandle abstraction
    config_resolver.py   # build_agent() resolves provider/agent, tools, approvals
    exceptions.py        # ToolApprovalDenied and future typed errors
    logging.py           # logging setup (text/json)
    models.py            # typed config models (moved from domain/)
    progress.py          # RichProgressReporter abstraction
    runner.py            # Unified run_turn (stream + tools + fallback)
    session.py           # ChatSession orchestrator (tool loop, streaming helpers)
    stdin_handler.py     # safe stdin read, truncation notice
  llm/
    litellm_client.py    # LiteLLMClient (LLMClient)
    chunk_utils.py       # extract_text for streaming chunks
    tool_adapter.py      # build LLM tool specs from MCP discovery
  mcp/
    client.py            # persistent sessions + low-level client logic
    manager.py           # MCPManager (MCPClient)
    api.py               # simple facade (list_tools, approvals)
    tools_resolver.py    # resolve MCP tools into ToolHandles
    builtin/
      __init__.py        # builtin registry (time, shell), discovery helpers
      time.py, shell.py  # builtin tools
  tests/                 # pytest unit tests for core, cli, llm, mcp, repl
pyproject.toml           # deps, ruff config
README.md
AGENTS.md                # (this file)
```

---
### Major Design/Implementation Points
- **Async everywhere**: All operations that might block (model calls, subprocesses, HTTP, MCP, etc) MUST use `asyncio`.
- **Config Management**: Merge global/user `~/.config/gptsh/config.yml` (plus any `~/.config/gptsh/config.d/*.yml` snippets) and project-local `./.gptsh/config.yml`. Per-project overrides global. Reference env vars as `${VAR_NAME}`. YAML supports a custom `!include` tag with wildcard patterns, resolved relative to the including file.
- **MCP Support**: Connects to MCP servers (local or remote). Servers can be configured inline via YAML under `mcp.servers` (recommended) or via a Claude-compatible `mcp_servers.json`. Inline `mcp.servers` takes precedence over files; per-agent overrides are supported via `agents.<name>.mcp.servers`.
- **CLI Only** (for now): All command, mode, config options via command-line interface. TUI reserved for later.
- **Tool Discovery**: Implements a `--list-tools` CLI arg to enumerate all tools provided by configured MCP servers, grouped by server.
- **Progress & Edge cases**: Use progress bar when possible. Always show progress or status updates for long ops, handle broken pipes, terminations, or timeouts gracefully.
 - **Agents**: Named presets defined in config (e.g., `default`, `code_reviewer`, `full_stack_developer`, `git_committer`) that specify a system prompt, model selection, and MCP tool policy. The `default` agent is used unless overridden by `--agent`.
- **Security**: Never log or print secrets or API keys. Use least-privilege principle for subprocesses and I/O. All configuration can include secrets via env variable references only, not hard-coded.
- **Error/Recovery**: Must auto-attempt reconnection if an MCP server is lost and auto-restart local ones if crashed.

#### Agent-Only Execution Model
- The CLI and core now operate exclusively on an Agent abstraction.
- `Agent` encapsulates:
  - a preconfigured `LiteLLMClient` with `base_params` (model and generation params),
  - an `ApprovalPolicy` (from merged global+agent MCP approvals),
  - resolved MCP `ToolHandle`s grouped by server (for discovery/listing).
- Core helpers:
- `core/runner.run_turn(agent, ...)` unified one-turn execution (streaming or non-streaming). If streamed tool_calls appear but no text, runner falls back to non-stream execution to run tools.
- `ChatSession.from_agent(agent, ...)` constructs a session using the agent’s `llm` and `policy`. In REPL, a single ChatSession is attached to `agent.session` and reused across turns.

#### Agent Resolution
- `build_agent(config, cli_*)` in `core/config_resolver.py`:
  - Resolves provider/agent precedence (CLI > agent > provider).
  - Builds `LiteLLMClient(base_params=...)` from model and generation params.
  - Computes effective tools policy (CLI flags and agent.tools), and resolves `ToolHandle`s via `mcp/tools_resolver.py`.
  - Builds `DefaultApprovalPolicy` via `mcp.api.get_auto_approved_tools`.
  - Returns an `Agent` object used by CLI and core execution.

#### MCP Tools
- `mcp/tools_resolver.resolve_tools(config, allowed_servers)` discovers available tools and returns `ToolHandle`s.
- `ToolHandle.invoke(args)` calls back into MCP execution (`execute_tool_async`).
- LLM tool specs for the chat loop are still produced via `llm/tool_adapter.py` inside `ChatSession`.

---
## Coding Conventions

- Async-first: any blocking I/O (HTTP, MCP, subprocess) must be async.
- Types: use full typing and from __future__ import annotations where practical.
- Imports: sorted/organized via Ruff’s isort (I001). Group stdlib, third-party, first-party.
- Line length: 100 (configured in [tool.ruff]). Prefer wrapping long strings, comments, and decorators.
- Black compatibility: E203 and related spacing handled by Ruff config; do not fight Black.
- Errors/exit codes: map to documented codes (0,1,4,124,130). Raise ToolApprovalDenied for required tools.
- Logging: never log secrets or headers; use core.logging.setup_logging. Redaction utility is planned.
- CLI: keep the entrypoint thin—delegate to core.api, core.session and core.utils.
- Tests: pytest with pytest-asyncio, structure tests near component domains.
- No license/copyright headers unless requested.
- Avoid one-letter variable names; prefer descriptive, short names.

---
## Linting & Testing (Ruff + Pytest)

- Primary linter: Ruff (configured in pyproject.toml; line-length=100; isort enabled).
- Run locally:
  - `UV_CACHE_DIR=.uv-cache uv pip install -e .[dev]`
  - `UV_CACHE_DIR=.uv-cache uv run ruff check`
  - `UV_CACHE_DIR=.uv-cache uv run pytest`
- Commit discipline: run Ruff and pytest for every change.

---
## Sandbox Guidance

- Prefer setting `UV_CACHE_DIR=.uv-cache` for uv commands to avoid home-directory permission issues in sandboxes.
- Examples:
  - `UV_CACHE_DIR=.uv-cache uv pip install -e .[dev]`
  - `UV_CACHE_DIR=.uv-cache uv run ruff check`
  - `UV_CACHE_DIR=.uv-cache uv run pytest -q`

---
## Configuration Schema and Examples

Config is merged in this order (later overrides earlier):
1. Global: `~/.config/gptsh/config.yml`
2. Global snippets: `~/.config/gptsh/config.d/*.yml` (merged in lexicographic order)
3. Project: `./.gptsh/config.yml`

Environment variables can be referenced using `${VAR_NAME}` syntax. YAML also supports a custom `!include` tag (with wildcard patterns) resolved relative to the including file. Secrets must be provided via env vars.

Example global config:
```yaml
default_agent: default
default_provider: openai

progress: true

providers:
  openai:
    api_key_env: OPENAI_API_KEY
    base_url: null       # set for custom endpoints / proxies
    extra_headers: {}

mcp:
  servers_files:    # Optional list of mcp server definitions
    - ~/.config/gptsh/mcp_servers.json
    - ./.gptsh/mcp_servers.json
  discovery_interval_seconds: 5

agents:
  default:
    model: gpt-4.1
    provider: openai
    prompt:
      system: "You are helpful assistant called gptsh"
      user: null    # Optional user prompt, if set agent will provide output to this user prompt right away
```

Example MCP servers file (Claude-compatible schema):
```json
{
  "mcpServers": {
    "tavily": {
      "transport": { "type": "sse", "url": "https://api.tavily.com/mcp" },
      "credentials": { "headers": { "Authorization": "Bearer ${TAVILY_API_KEY}" } }
    },
    "filesystem": {
      "transport": { "type": "stdio" },
      "command": "uvx",
      "args": ["mcp-filesystem", "--root", "."],
      "env": {}
    }
  }
}
```

For better examples, see examples directory

Notes:
- Precedence: CLI `--mcp-servers` paths > agent `mcp.servers` > global `mcp.servers` > servers file (./.gptsh/mcp_servers.json, then ~/.config/gptsh/mcp_servers.json).
- `mcp.servers` accepts either a YAML mapping or a JSON string. If a JSON string contains a top-level `mcpServers` key, it is unwrapped automatically.
- `${VAR}` inside inline JSON or files is expanded from the runtime environment (and `${env:VAR}` is normalized to `${VAR}`).
- Built-in in-process servers `time` and `shell` are always available by default and merged even when using inline `mcp.servers` or agent overrides; you can disable them by defining servers with the same names and a `disabled: true` flag.

---
## Async Execution Model

- Standardize on `asyncio` with `httpx.AsyncClient` for HTTP/SSE and async subprocess for stdio.
- All long-running calls must accept a timeout (from config) and be cancellable.
- Graceful shutdown: cancel tasks, close streams/clients, terminate child processes.
- Streaming: Runner uses `ChatSession.prepare_stream` and `stream_with_params` and logs streamed tool_call deltas; if the model produces no text but tools are requested, it falls back to a non-stream turn to execute tools and print the result.

---
### LLM Session Reuse and Streaming Usage/Cost

- Runner-owned lifecycle:
  - The runner constructs and starts a ChatSession when needed and closes it only if it created it.
  - In REPL mode a single ChatSession is reused across turns per agent; the runner receives a prebuilt session and does not close it.
- ChatSession responsibilities:
  - ChatSession does not call start()/close() inside `stream_turn`; the caller (runner/REPL) controls lifecycle.
  - Tool discovery/specs remain cached on the ChatSession for the duration of its life.
- Streaming usage and cost:
  - LiteLLM client sets `stream_options: { include_usage: true }` for streamed calls so the final streamed chunk carries a real `usage` object (prompt, completion, total tokens).
  - To include pricing-derived `usage.cost`, following is configured: `litellm.include_cost_in_streaming_usage = True`.
  - Usage is read from the final streamed chunk (`chunk.usage`). Do not rely on `_hidden_params["usage"]` when `include_usage` is enabled.
- Provider client/session reuse (no LiteLLM cache required):
  - Keep a single provider client per Agent (e.g., OpenAI Async client or aiohttp shared session) and pass it on each turn.
  - Recommended wiring:
    - Store the provider client on the Agent/LLM implementation (e.g., `agent.llm.client` or `agent.llm.shared_session`).
    - In `ChatSession._prepare_params`, attach it to request params: `params["client"] = agent.llm.client` or `params["shared_session"] = agent.llm.shared_session` when present.
  - This preserves upstream HTTP connection reuse and any provider-side token accounting heuristics across turns, without enabling optional LiteLLM-internal caching.
- REPL teardown:
  - On REPL exit, close any cached ChatSessions to flush MCP and provider clients cleanly.

---
## MCP Lifecycle and Resilience

- Discovery: periodically list and cache tools from all configured MCP servers.
- Remote servers: auto-reconnect with exponential backoff and jitter; retry indefinitely unless `max_retries` set.
- Local/stdio servers: supervise subprocesses; restart on exit; respect backoff.
- Readiness: after (re)connect/spawn, perform `list_tools()` or configured healthcheck before marking available.
- SSE reconnect: resume streams when possible; on failure, re-list tools to resync.
- Docker support: allow servers to be spawned via `command: docker` with provided args. Ensure `--health-cmd` if possible, or rely on list_tools readiness check.

---
## Logging and Telemetry

- Use Python `logging` with levels configured via config; default `WARNING` (override with -v/--verbose or --debug).
- Support `text` and `json` formats; prefer `json` when not attached to a TTY.
- Never log secrets; redact known keys and header values.
- Include request IDs and timestamps for correlation; do not enable telemetry without explicit opt-in.

---
## Stdin Handling Strategy

- If stdin is non-tty, read up to `stdin.max_bytes`.
- If input exceeds limit, truncate and append a notice to the text. A summarization strategy may be added in the future.
- Treat binary input as bytes; attempt `utf-8` with `errors="replace"` and label garbled sections.

---
## Tool Discovery and Selection

- `--list-tools` prints tools grouped by MCP server.
- Tool invocation is guided by the LLM; default tool_choice is 'auto'. You can set the agent parameter 'tool_choice' to influence behavior.
- Optional filtering via `approvals.allow_patterns`/`deny_patterns` to constrain accessible tools.

---
## Security and Approvals

- Read-only operations proceed automatically.
- Destructive or system-changing tools (e.g., file write, process exec, network-modifying actions) require explicit confirmation unless overridden by config.
- Never execute shell commands on behalf of tools without an approval gate.

---
## Testing Strategy

- Unit tests with `pytest` and `pytest-asyncio` for async flows.
- Integration tests spinning mock MCP servers (stdio and SSE/HTTP) and chaos tests (kill/restart servers) to validate recovery.
- Snapshot tests for CLI output (TTY vs non-TTY).
- Run via `uv run pytest`.
 
### Running Tests

To run the full test suite:

```bash
uv venv
uv pip install -e .[dev]
uv run pytest
```

You can customize pytest execution, for example:

```bash
uv run pytest --maxfail=1 --disable-warnings -q
```

---
## Packaging and uv Workflow

- Use a single `pyproject.toml` managed by `uv`.
- Console entry point: `[project.scripts] gptsh = "gptsh.cli.entrypoint:main"`.
- Editable install for development: `uv venv && uv pip install -e .[dev]`.
- Run: `uv run gptsh "your prompt"` (ensures the right venv/tooling is used).
- Testing: `uv run pytest`.
- Optional: `uv lock` to pin resolutions and achieve reproducible builds.

Example pyproject excerpt:
```toml
[project]
name = "gptsh-cli"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
  "litellm",
  "click",
  "pyyaml",
  "rich",
  "httpx",
  "python-dotenv",
  "mcp>=1.15.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[project.scripts]
gptsh = "gptsh.cli.entrypoint:main"
```

---
## Exit Codes

- `0`   success
- `1`   generic failure
- `2`   configuration error (invalid/missing)
- `3`   MCP connection/spawn failure (after retries)
- `4`   tool approval denied
- `124` operation timeout
- `130` interrupted (Ctrl-C)

---
## Token Budgeting and Rate Limiting

- Use provider-specific token limits; keep a safety margin (e.g., 20%).
- When stdin or retrieved context exceeds budget: summarize then proceed.
- Configure `max_tokens` and `temperature` via config; use `litellm` token counting when available.
- Handle 429/5xx with exponential backoff and jitter; cap retries or respect provider `Retry-After`.

---
## Approval UX

- When a tool is flagged as destructive, prompt: `Allow tool <name> with args <...>? [y/N]`.
- Support non-interactive mode via config to auto-deny or auto-approve specific allow-list patterns.
- Timeout approval prompts with a default deny.

---
 
## CLI Usage

- `gptsh [PROMPT]`         — Request answer using config/default agent; interactive prompt.
- `dmesg | gptsh "Find errors in this input"`  — Uses stdin as chat/file context.
- `gptsh "Find python source files in this project"`  — Calls MCP filesystem tool.
- `gptsh --list-tools`     — Lists all available MCP tools by server.

**Global CLI Options:**
- `--provider NAME`                # Select provider from config (overrides default)
- `--model MODEL`                  # Specify LLM model (overrides config/agent)
- `--agent NAME`                   # Select named agent from config (default: "default")
- `--config PATH`                  # Path to config file
- `--stream/--no-stream`           # Stream LLM output (default: on)
- `--progress/--no-progress`       # Show progress spinners (default: on; rendered to stderr)
- `--debug`                        # Enable DEBUG logging
- `-v, --verbose`                  # Enable INFO logging
- `--mcp-servers PATHS`            # Comma/space-separated path(s) to MCP servers JSON
- `--list-tools`                   # Print all discovered MCP tools, grouped by server
- `--list-providers`               # List configured providers
- `-o, --output [text|markdown]`   # Output format (default: markdown)
- `--no-tools`                     # Disable MCP (discovery and execution)
- `--tools LABELS`                 # Comma/space-separated allow-list of MCP servers to load
- `-h, --help`

Notes:
- The CLI always resolves an `Agent` using `build_agent` before running. Non-stream and stream paths both use `ChatSession.from_agent`.
- `--no-tools` and `--tools` influence the resolved agent’s tools via `compute_tools_policy`.
- REPL module lives under `gptsh/cli/repl.py` and updates the active model and parameters via `agent.llm._base` during the session.

---
## Installation and Development

- **All installation, dev, and test scripts must use `uv`/`uvx` in a managed virtual environment.** 
- Example: `uv venv` to init, then `uv pip install -e .` to install deps, `uv run pytest`, etc.

---
## Reference Docs
- [litellm Documentation](https://docs.litellm.ai/)
- [litellm MCP Integration](https://docs.litellm.ai/docs/mcp)
- [Model Context Protocol (MCP) docs](https://modelcontextprotocol.io/introduction)
- [OpenAI Agents SDK MCP](https://openai.github.io/openai-agents-python/mcp/)
- [Claude's MCP Tool Format](https://github.com/modelcontextprotocol/servers)
- [Click (CLI)](https://click.palletsprojects.com/)
- [pyyaml](https://pyyaml.org/)
- [uv project](https://github.com/astral-sh/uv)

---
## AGENT-FACING GUIDANCE

### Development Instructions

1) Keep config, security, and extensibility as priorities in any new code.
2) Prefer async/await for concurrency — any blocking work (HTTP, MCP, subprocess, file IO) must be async.
3) MCP lifecycle must be resilient (spawn, respawn, reconnect with backoff) and tolerate temporary unavailability.
4) Read config from both global and per-project sources; project overrides global.
5) Secrets/API keys must come only from environment variables; never hard-code or log them.
6) Minimize dependencies; add new ones only if essential and vetted for security/maintainability.
7) Use `uv`/`uvx` for all dev, install, and test commands.

### Safety Rules

- Approvals: destructive/system-changing tools require explicit user approval unless auto-approved by config. Respect non-TTY auto-deny defaults.
- Shell execution: never execute shell commands on behalf of tools without passing the approval gate. In CLI agent modes, follow the tool-specific instructions (execute via tool when available, otherwise emit a POSIX-safe command only).
- Secrets: do not log API keys, headers, request bodies containing secrets, or environment values. Use configured logging utilities and redaction helpers when available.
- Timeouts and cancellation: ensure long-running operations accept timeouts and are cancellable; fail safe with clear exit codes.

### Memory and Planning

- We are using **serena** tools to work with memories
- Maintain persistent memories during multi-step work:
  - project_overview: high-level architecture, modules, recent refactors
  - refactor_progress: ongoing changes, rationale, and next steps
  - task_completion_checklist: checklist of goals per task with completion marks
  - style_and_conventions: linting, typing, logging, and code style rules
  - suggested_commands: frequently used uv/lint/test commands
- Create task-specific memories for larger features/PRs and update them at meaningful checkpoints (after refactors, API changes, or CLI behavior updates).
- Planning: use the **sequentialthinking** tool for multi-step planning, design trade-offs, and verification. Keep plans concise but actionable; verify outcomes before marking done.

### Suggested Commands (Ruff/Pytest/UV)

- Initialize dev env: `UV_CACHE_DIR=.uv-cache uv pip install -e .[dev]`
- Lint: `UV_CACHE_DIR=.uv-cache uv run ruff check`
- Tests: `UV_CACHE_DIR=.uv-cache uv run pytest`
- CLI help: `UV_CACHE_DIR=.uv-cache uv run gptsh --help`
- Optional auto-fix: `UV_CACHE_DIR=.uv-cache uv run ruff check --fix`

### Task Completion Checklist

- Lint clean: `UV_CACHE_DIR=.uv-cache uv run ruff check` passes (or waivers documented).
- Tests pass: `UV_CACHE_DIR=.uv-cache uv run pytest`.
- Docs updated: README.md and AGENTS.md reflect structural/workflow changes.
- Memories updated
- Config updated: pyproject.toml for tool config (ruff) and dev deps as needed.
- Security: no secrets in logs or code; approval prompts are TTY-safe.
- Architecture: CLI entrypoint remains thin; complex logic in core modules.
- Code quality: new code is typed and async-safe.

---

This file should be updated as additional requirements or architecture decisions are made.
