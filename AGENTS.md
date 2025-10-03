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
- **Config Management**: Merge global/user `~/.config/gptsh/config.yml` (plus any `~/.config/gptsh/config.d/*.yml` snippets) and project-local `./.gptsh/config.yml`. Per-project overrides global. Reference env vars as `${VAR_NAME}`. YAML supports a custom `!include` tag with wildcard patterns, resolved relative to the including file.
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
2. Global snippets: `~/.config/gptsh/config.d/*.yml` (merged in lexicographic order)
3. Project: `./.gptsh/config.yml`

Environment variables can be referenced using `${VAR_NAME}` syntax. YAML also supports a custom `!include` tag (with wildcard patterns) resolved relative to the including file. Secrets must be provided via env vars.

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

## Refactor: Modular, Extensible Architecture

### Overview

GPTSH will be refactored into a clean, modular structure to maximize readability, testability, and extensibility. The new layers and their responsibilities are:

- **Interfaces**: Define Protocols for core abstractions—LLM clients, MCP clients, approval policies, progress reporters, and UI entrypoints.
- **Domain Models**: Typed `@dataclass` definitions for AgentConfig, ProviderConfig, ToolSpec, ToolServerSpec, and session state.
- **LLM Adapters**: Encapsulate all LiteLLM interactions in `gptsh/llm/litellm_client.py` with `LiteLLMClient`, plus the tool-adapter in `gptsh/llm/tool_adapter.py`.
- **MCP Management**: Centralize server discovery, lifecycle, and tool invocation in `gptsh/mcp/manager.py` with `MCPManager`. Builtin tools use a decorator registry in `gptsh/mcp/builtin/base.py`.
- **Orchestration**: The `ChatSession` in `gptsh/core/session.py` drives message history, tool loops (parallel-capable), approvals, and progress updates.
- **Approval Policy**: `DefaultApprovalPolicy` in `gptsh/core/approval.py` handles wildcard auto-approval and interactive confirmations via Rich.
- **Progress Reporting**: `RichProgressReporter` in `gptsh/core/progress.py` provides persistent, wrapping-safe task lines.
- **UI Layer**: Abstract UI with `UIInterface` in `gptsh/interfaces.py`; implement CLI in `gptsh/ui/cli.py` and scaffold TUI in `gptsh/ui/tui.py`.
- **Configuration & Logging**: Keep the loader in `gptsh/config/loader.py` with !include and env-var expansion, and centralized logging/redaction in `gptsh/core/logging.py`.

This architecture ensures each component can be replaced or extended independently—swap LLM backends, add a web UI, or plug in new tool servers without touching unrelated code.

To support future extensibility—multiple front-ends (CLI, TUI, web), pluggable LLM backends, flexible MCP tool servers, and structured domain models—GPTSH will be reorganized into a modular, object-oriented architecture. Each layer exposes a clear interface (Protocol or abstract base class), and dedicated implementation modules fulfill those interfaces. This structure improves readability, testability, security, and maintainability, and enables independent replacement of components without impacting others.

**Core Components**

1. **Interfaces** (`gptsh/interfaces.py`):  
   - `LLMClient`: async `complete(params) -> Dict[str,Any]` and `stream(params) -> AsyncIterator[str]`.  
   - `MCPClient`: async `list_tools() -> Dict[str,List[str]]` and `call_tool(server,tool,args) -> str`.  
   - `ApprovalPolicy`: `is_auto_allowed(server,tool) -> bool` and async `confirm(server,tool,args) -> bool`.  
   - `ProgressReporter`: `start()`, `stop()`, `add_task(desc) -> Optional[int]`, `complete_task(id,desc)`, `pause()`, `resume()`.

2. **LLM Adapter** (`gptsh/llm/litellm_client.py`):  
   - `LiteLLMClient` implements `LLMClient`, centralizing all `litellm` calls and chunk parsing.

3. **MCP Manager** (`gptsh/mcp/manager.py`):  
   - `MCPManager` implements `MCPClient`, wrapping existing logic with a clean API: `start()`, `list_tools()`, `call_tool()`, `stop()`.

4. **Approvals** (`gptsh/core/approval.py`):  
   - `DefaultApprovalPolicy` implements `ApprovalPolicy`, encapsulating wildcard normalization and interactive confirmation via `rich.prompt`.

5. **Progress Reporting** (`gptsh/core/progress.py`):  
   - `RichProgressReporter` implements `ProgressReporter`, providing persistent, well-wrapped progress bars.

6. **Chat Orchestration** (`gptsh/core/session.py`):  
   - `ChatSession` class encapsulates conversation history, LLM calls, tool-call detection, parallel execution, approvals, and progress updates—replacing `complete_with_tools`.

7. **Domain Models** (`gptsh/domain/models.py`):  
   - Typed `@dataclass` definitions (`ProviderConfig`, `AgentConfig`, `ToolSpec`, etc.), mapping raw config dicts to structured objects.

8. **UI Layer** (`gptsh/ui`):  
   - `UIInterface` protocol.  
   - `gptsh/ui/cli.py` and future `gptsh/ui/tui.py` implement `UIInterface`.  
   - `cli/entrypoint.py` selects and invokes the chosen UI.

9. **Builtin Tool Registry** (`gptsh/mcp/builtin/base.py`):  
   - Decorator-based `@tool` registration and shared `list_tools`, `list_tools_detailed`, and `execute` dispatch.  
   - Individual builtin modules only declare tool functions with metadata.

### TODO List

- [ ] Create `gptsh/interfaces.py` defining `LLMClient`, `MCPClient`, `ApprovalPolicy`, `ProgressReporter`, and `UIInterface` protocols.  
- [ ] Implement `gptsh/llm/litellm_client.py` with `LiteLLMClient` and `_extract_text` logic.  
- [ ] Develop `gptsh/llm/tool_adapter.py` for MCP tool discovery and call parsing.  
- [ ] Extract `_MCPManager` into `gptsh/mcp/manager.py` and expose clean `MCPClient` API.  
- [ ] Introduce `gptsh/mcp/builtin/base.py` decorator registry and refactor existing builtin modules (`shell.py`, `time.py`).  
- [ ] Build `gptsh/core/approval.py` with `DefaultApprovalPolicy` (wildcard + interactive confirm).  
- [ ] Build `gptsh/core/progress.py` with `RichProgressReporter` for persistent, wrapped progress tasks.  
- [ ] Create `gptsh/domain/models.py` with typed dataclasses for config and runtime models.  
- [ ] Implement `ChatSession` in `gptsh/core/session.py` to orchestrate LLM calls, tool loops, approvals, and progress.  
- [ ] Create `gptsh/ui/cli.py` implementing `UIInterface`.  
- [ ] Update `cli/entrypoint.py` to instantiate and wire `LiteLLMClient`, `MCPManager`, `DefaultApprovalPolicy`, `RichProgressReporter`, and `ChatSession`.  
- [ ] Deprecate legacy functions (`complete_with_tools`, etc.) with thin wrappers for backward compatibility.  
- [ ] Add unit tests for all new components and their protocols.  
- [ ] Update `README.md` and code comments to explain the new modular architecture.  
- [ ] Plan and document a stepwise migration strategy that preserves CI passing at each stage.

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
- [x] Interactive REPL mode with colored "<agent>|<model>>" prompt
- [x] Maintain per-REPL session chat history (messages threaded through LLM calls)
- [x] Interactive mode accepts initial prompt from stdin or positional arg and continues after response
- [x] Persistent MCP sessions across the entire REPL; initialized once and cleaned up on exit; LiteLLM async client cleanup and warning suppression
- [x] REPL slash-commands: `/exit`, `/quit`, `/model <model>`, `/agent <agent>`, `/reasoning_effort [minimal|low|medium|high]`
- [x] Tab-completion for slash-commands and agent names; switching agent also updates model and reloads tools
- [x] Add progress bars and status feedback for long-running operations
- [x] Build interactive approval UX for destructive or privileged tool invocations
- [x] Support full agent presets (system/user prompts, model selection, tool policies)
- [x] Complete configuration overrides and merging for global and project-local settings

### Pending / Roadmap

#### MCP Integration
- [ ] Improve MCP lifecycle resilience (auto-respawn, backoff, health checks)
- [ ] Add option to define mcpServers in gptsh config per agents either by passing structure or including mcp_servers.json file

#### User Experience
- [ ] Detect TTY vs non-TTY and provide appropriate UI modes (spinner vs minimal output)
- [ ] Add session history, introduce new `-s [session]` and `--list-sessions` parameters

#### Configuration & Agents
- [ ] Implement log redaction for secrets and sensitive data
- [ ] Support logging into file

#### Workflows
- [ ] Add support for Workflows.

Workflow is using multiple steps that consists of agents and other execution blocks

Example config defining `create-git-commit` workflow that is using already defined `committer` agent and shell execution steps:

```yaml
workflow:
  create-git-commit:
    parameter:
      amend:
        # Will pass amend into workflow steps (shell variable of value 0/1 and into LLM step as user message)
        type: bool
      push:
        type: bool
    steps:
      - description: "Obtain diff of changes"
        on_failure: exit # Can be "exit" (default, will fail and pass output and exitcode) or "continue"
        shell:
          command:
            staged=$(git diff --cached)

            if [[ -z "$staged" && $amend -eq 0 ]]; then
              echo "No staged changes to commit!" >&2
              exit 1
            fi

            if [[ $amend -eq 1 ]]; then
              if [[ -z "$staged" ]]; then
                # No staged changes, just reword last commit
                git show --pretty=format:%B HEAD
              else
                # Staged changes and committed changes
                git show --pretty=format:'' HEAD
                echo "$staged"
              fi
            else
              echo "$staged"
            fi
      - description: "Generate commit message"
        llm:
          agent: committer
          prompt:
            user: |
              Generate single commit message based on provided diff:
              ${step[-1].stdout}
      - description: "Commit and push"
        shell:
          env:
            # define msg passed as env variable, reference last step's stdout.
            msg: "${step[-1].stdout}"
          command: |
            if [[ $amend -eq 1 ]]; then
              git commit --amend --edit -m "$msg"
            else
              git commit --edit -m "$msg"
            fi

            if [[ $push -eq 1 ]]; then
              upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u})
              remote=${upstream%%/*}
              branch=${upstream#*/}
              git push "$remote" HEAD:"$branch"
            fi
```

Usage will be very simple:
```sh
gptsh -w create-git-commit
# Or with parameters
gptsh -w create-git-commit --amend --push
```

#### Resilience & Timeouts
- [ ] Add request timeouts with exponential backoff and jitter for model and MCP calls

#### Packaging & Workflow
- [ ] Add `uv lock` workflow and packaging pipelines for releases
- [ ] Add more full tests coverage

#### Code quality & Optimization & Performance
- [ ] Cleanup code, deduplicate some parts (loading agents, etc.), use objects

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
