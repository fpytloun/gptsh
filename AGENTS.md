# GPTSH AGENTS DEVELOPMENT GUIDE

This document describes the requirements, architecture, and development instructions for AI agents building and maintaining the `gptsh` shell client.

---

## Project Vision

A modular, extensible, and secure Python shell client that empowers developers and system administrators with conversational and tool-augmented AI using the latest GPT-like LLMs, based on [litellm](https://docs.litellm.ai/). It natively supports the Model Context Protocol (MCP) for tool integrations (including Claude and others)

---
## Tech Stack
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
- **watchdog** for config reloads (optional)
- **python-dotenv/os** for environment variable expansion in configs

---
## Project Structure
```
gptsh/
  cli/         # CLI entrypoint, argument parsing (with click)
  config/      # Load, merge, validate configs from ~/.config/gptsh & ./.gptsh/
  core/        # Shell/LLM/MCP client logic (async, robust)
  mcp/         # MCP-handling: protocol impl, server mgmt, recovery
  plugins/     # (Future) Extension base and built-in plugins
  tui/         # (Future) Optional TUI modules
  utils/       # Utilities, helpers
  tests/       # pytest-based test suite
pyproject.toml # uv/uvx uses this
README.md
AGENTS.md      # (this file)
```

---
## Major Design/Implementation Points
- **Async everywhere**: All operations that might block (model calls, subprocesses, HTTP, MCP, etc) MUST use `asyncio`.
- **Config Management**: Merge global/user `~/.config/gptsh/config.yml` and project-local `.gptsh/config.yml`. Per-project overrides global. Reference env vars as `${VAR_NAME}`.
- **MCP Support**: Connects to MCP servers (local or remote), managed/configured via `mcp_servers.json` (Claude-compatible). Auto-respawn/reconnect logic for local/remote MCP servers, with exponential backoff and progress feedback.
- **CLI Only** (for now): All command, mode, config options via command-line interface. TUI reserved for later.
- **Tool Discovery**: Implements a `--list-tools` CLI arg to enumerate all tools provided by configured MCP servers, grouped by server.
- **Progress & Edge cases**: Use progress bar when possible. Always show progress or status updates for long ops, handle broken pipes, terminations, or timeouts gracefully.
 - **Agents**: Named presets defined in config (e.g., `default`, `code_reviewer`, `full_stack_developer`, `git_committer`) that specify a system prompt, model selection, and MCP tool policy. The `default` agent is used unless overridden by `--agent`.
- **Security**: Never log or print secrets or API keys. Use least-privilege principle for subprocesses and I/O. All configuration can include secrets via env variable references only, not hard-coded.
- **Error/Recovery**: Must auto-attempt reconnection if an MCP server is lost and auto-restart local ones if crashed.

---
## Configuration Schema and Examples

Config is merged in this order (later overrides earlier):
1. Global: `~/.config/gptsh/config.yml`
2. Project: `./.gptsh/config.yml`

Environment variables can be referenced using `${VAR_NAME}` syntax. Secrets must be provided via env vars.

Example global config:
```yaml
default_agent: default
default_provider: openai

progress: true
timeouts:
  request_seconds: 60
stdin:
  max_bytes: 5242880   # 5 MiB
  overflow_strategy: summarize  # summarize | truncate
logging:
  level: info          # debug|info|warning|error
  format: text         # text|json
  redact_keys: ["api_key", "authorization"]

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
  approvals:
    allow_patterns: []   # optional whitelist by tool name pattern
    deny_patterns: []    # optional blacklist by tool name pattern
    ask_patterns: ["*"]  # list of tools to always ask before calling, by default it will always ask
  reconnect:
    initial_backoff: 0.5
    max_backoff: 30
    jitter: true
    max_retries: 0       # 0 = unlimited
  spawn:
    enabled: true
    default_env: {}
    healthcheck:
      type: list_tools   # list_tools | http | none
      timeout: 10

agents:
  default:
    model: gpt-4.1
    provider: openai
    prompt:
      system: "You are helpful assistant called gptsh"
      user: null    # Optional user prompt, if set agent will provide output to this user prompt right away
```

Example per-project config (`.gptsh/config.yml`):
```yaml
model: gpt-4o-mini
stream: true
litellm:
  api_key_env: OPENAI_API_KEY
mcp:
  tool_choice: required
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

Notes:
- A single servers file is selected by precedence: CLI mcp.servers_files (first existing), then ./.gptsh/mcp_servers.json, then ~/.config/gptsh/mcp_servers.json.
- `${VAR}` inside servers JSON is expanded from the runtime environment (and `${env:VAR}` is normalized to `${VAR}`).
- Built-in in-process servers `time` and `shell` are always available by default and can be referenced or disabled via mcp_servers.json.

---
## Async Execution Model

- Standardize on `asyncio` with `httpx.AsyncClient` for HTTP/SSE and async subprocess for stdio.
- All long-running calls must accept a timeout (from config) and be cancellable.
- Graceful shutdown: cancel tasks, close streams/clients, terminate child processes.
- Streaming: prefer server-sent tokens; if disabled, buffer and print at end.

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
- Run via `uvx pytest`.
 
### Running Tests

To run the full test suite:

```bash
uv venv
uv pip install -e .[dev]
uvx pytest -q
```

You can customize pytest execution, for example:

```bash
uvx pytest --maxfail=1 --disable-warnings -q
```
 
 ## TODO / Implementation Status
 
 ### Completed
 - [x] Scaffolded project structure and Python packages (`cli`, `config`, `core`, `mcp`, `plugins`, `utils`, `tests`)
 - [x] Implemented config loader with YAML merging and environment variable expansion
 - [x] Built CLI entry point with Click, supporting prompt, agent, model, stream, progress, debug flags
 - [x] Integrated logging setup with configurable levels and formats
 - [x] Added stdin handler with truncation strategy for piped input
 - [x] MCP tool discovery and execution via official MCP SDK (stdio/HTTP/SSE)
 - [x] Implemented asynchronous LLM calls via `litellm` with streaming and single-shot support
 - [x] Wrote pytest tests for config loader, stdin handler, and CLI flows
 - [x] Added --mcp-servers CLI option to override path to MCP servers file
 - [x] Added --provider option to select LiteLLM provider from config
 - [x] Added --list-providers CLI option to list configured providers
 - [x] Rich Progress UI rendered to stderr; clean teardown before output
 - [x] Output format flag `-o/--output` (text|markdown) with Markdown rendering at end
 - [x] `--no-tools` to disable MCP; `--tools` to whitelist allowed MCP servers
 
 ### Pending / Roadmap

 #### MCP Integration
 - [ ] Improve MCP lifecycle resilience (auto-respawn, backoff, health checks)
 - [ ] Create integration and chaos tests for MCP lifecycle resilience

 #### User Experience
 - [x] Add progress bars and status feedback for long-running operations
 - [ ] Build interactive approval UX for destructive or privileged tool invocations
 - [ ] Detect TTY vs non-TTY and provide appropriate UI modes (spinner vs minimal output)

 #### Configuration & Agents
 - [ ] Support full agent presets (system/user prompts, model selection, tool policies)
 - [ ] Complete configuration overrides and merging for global and project-local settings
 - [ ] Implement log redaction for secrets and sensitive data

 #### Resilience & Timeouts
 - [ ] Add request timeouts with exponential backoff and jitter for model and MCP calls

 #### Packaging & Workflow
 - [ ] Add `uv lock` workflow and packaging pipelines for releases
 
 ---
 ## Packaging and uv Workflow

- Use a single `pyproject.toml` managed by `uv`.
- Console entry point: `[project.scripts] gptsh = "gptsh.cli.entrypoint:main"`.
- Editable install for development: `uv venv && uv pip install -e .[dev]`.
- Run: `uv run gptsh "your prompt"` (ensures the right venv/tooling is used).
- Testing: `uvx pytest -q`.
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

- `gptsh [PROMPT]`         — Request answer using config/default GPT model; interactive prompt.
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

---
## Installation and Development

- **All installation, dev, and test scripts must use `uv`/`uvx` in a managed virtual environment.** 
- Example: `uv venv` to init, then `uv pip install -e .` to install deps, `uvx pytest`, etc.

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
## LLM Agent Development Instructions

1. Always keep config, security, and extensibility as priorities in any new code.
2. Prefer async/await for concurrency — all code that can block must be async.
3. All MCP server lifecycle handling (spawn, respawn, reconnect) must be resilient — handle restarts and temporary unavailability.
4. Always read config from both global and per-project sources; project overrides global.
5. Secrets/API keys must only come from env vars, not static config.
7. Do not add other dependencies unless absolutely required and review for security/maintainability.
8. All development, installation, and tests must use `uv`/`uvx` commands.

---

This file should be updated as additional requirements or architecture decisions are made.
