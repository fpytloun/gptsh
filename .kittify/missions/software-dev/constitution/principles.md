# gptsh Constitution

## Core Principles

### I. Async-First Architecture (NON-NEGOTIABLE)
**All blocking operations MUST be async**
- HTTP, MCP, subprocess, file I/O must use `asyncio`
- All operations accept timeouts and are cancellable
- Graceful shutdown: cancel tasks, close streams/clients, terminate child processes
- Runner-owned lifecycle for sessions; REPL reuses single session per agent
- No blocking calls in event loops; use `asyncio.to_thread` for sync-only operations

**Rationale**: gptsh must handle multiple concurrent operations (LLM streams, MCP servers, tool execution) without blocking. Async-first ensures responsiveness and scalability.

### II. Type Safety and Code Quality
**Python 3.10+ with full typing**
- Use `from __future__ import annotations` where practical
- All functions/methods must have type hints for parameters and return values
- Prefer descriptive variable names; avoid one-letter variables
- Ruff (line-length=100) as primary linter with isort enabled
- Import order: stdlib, third-party, first-party (enforced by Ruff I001)
- Black-compatible spacing (E203 handled in Ruff config)

**Enforcement**:
- Pre-commit: `UV_CACHE_DIR=.uv-cache uv run ruff check` must pass
- CI/CD: Ruff check is a required gate

### III. Test-First Development (MANDATORY)
**Tests must precede implementation**
- Unit tests with `pytest` and `pytest-asyncio` for all async flows
- Integration tests for MCP server interactions (stdio and SSE/HTTP)
- Chaos tests for resilience (kill/restart servers, network failures)
- Snapshot tests for CLI output (TTY vs non-TTY behavior)
- Minimum 80% code coverage for new code paths

**Test Structure**:
- Mirror source structure: `gptsh/tests/test_<module>.py`
- Use fixtures from `conftest.py` for shared state isolation
- Mock external dependencies (LLM APIs, MCP servers) in unit tests
- Use `NoOpProgressReporter` for tests to avoid UI noise

**Red-Green-Refactor Cycle**:
1. Write failing test demonstrating requirement
2. Get user approval for test design
3. Verify test fails (RED)
4. Implement minimal code to pass (GREEN)
5. Refactor while keeping tests green

**Running Tests**:
```bash
UV_CACHE_DIR=.uv-cache uv run pytest
UV_CACHE_DIR=.uv-cache uv run pytest --maxfail=1 --disable-warnings -q
```

### IV. Security & Secrets Management
**Never expose secrets**
- API keys/tokens MUST come from environment variables only
- Use `${VAR_NAME}` syntax in config; expand at runtime
- Never log secrets, headers, request bodies, or env values
- Redaction utility for sensitive data in logs (planned feature)
- DefaultApprovalPolicy for destructive operations

**Approval Gates**:
- Read-only operations: proceed automatically
- Destructive/system-changing tools: require explicit confirmation unless auto-approved
- Non-TTY mode: auto-deny destructive operations by default
- Timeout approval prompts with default deny
- Never execute shell commands without passing approval gate

**Security Checklist**:
- [ ] No hard-coded secrets in code or config
- [ ] Logging statements checked for secret leakage
- [ ] Approval prompts are TTY-safe
- [ ] Subprocess execution uses least-privilege principle

### V. User Experience Consistency
**Predictable I/O contract**
- **stdin**: Non-interactive stdin read and appended to prompt; binary auto-detected; switch to /dev/tty for REPL
- **stdout**: Only LLM output (clean, pipeable)
- **stderr**: Progress bars, approval prompts, logs

**Output Formats**:
- Markdown (default): Rich rendering via `rich` library
- Text: Plain output for scripting/piping
- Format selected via `-o/--output` flag

**Progress & Status**:
- Show progress spinners for long operations (stderr)
- Disable with `--no-progress` for scripting
- Handle broken pipes, terminations, timeouts gracefully
- Exit codes: 0=success, 1=generic, 2=config, 3=MCP failure, 4=approval denied, 124=timeout, 130=interrupted

**Error Messages**:
- Clear, actionable error messages
- Include context (what failed, why, how to fix)
- Log full stack traces only in --debug mode

### VI. Configuration & Extensibility
**Hierarchical config merge**
- Global: `~/.config/gptsh/config.yml`
- Global snippets: `~/.config/gptsh/config.d/*.yml` (lexicographic order)
- Project: `./.gptsh/config.yml` (overrides global)
- Custom `!include` tag with wildcard support (relative to including file)

**Agent-Based Architecture**:
- Agents are named presets (system prompt + model + tools)
- Agent encapsulates: LiteLLMClient, ApprovalPolicy, ToolHandles
- `build_agent()` resolves provider/agent precedence: CLI > agent > provider
- Per-agent tool allow-lists and MCP server overrides

**MCP Configuration Precedence**:
1. CLI `--mcp-servers`
2. Per-agent `agents.<name>.mcp.servers`
3. Global `mcp.servers`
4. Servers file (`./.gptsh/mcp_servers.json` then `~/.config/gptsh/mcp_servers.json`)

**Built-in Tools**:
- `time` and `shell` always available (in-process)
- Can be disabled per-agent via `tools` allow-list

### VII. Performance Requirements
**Response time targets**:
- CLI startup: < 500ms (cold start)
- Config loading: < 100ms
- Tool discovery: < 2s (per MCP server)
- Streaming first token: < 2s (LLM-dependent)

**Resource Management**:
- Provider client/session reuse per Agent
- Single HTTP client shared across turns (avoid connection overhead)
- Token budgeting: 20% safety margin from provider limits
- Graceful degradation when rate-limited (exponential backoff + jitter)

**Memory**:
- Session history compaction: `/compact` command in REPL
- Multimodal content stored as text markers (no base64 in saved sessions)
- Limit stdin read to `stdin.max_bytes` with truncation notice

### VIII. MCP Lifecycle Resilience
**Auto-recovery for all MCP servers**
- Remote servers: auto-reconnect with exponential backoff + jitter
- Local/stdio servers: supervise subprocesses, restart on exit
- Readiness check: `list_tools()` after (re)connect before marking available
- SSE: resume streams when possible; re-list tools on failure
- Docker support: spawn via `command: docker` with health checks

**Discovery**:
- Periodic tool discovery at `discovery_interval_seconds`
- Cache tools per ChatSession lifetime
- Graceful handling of temporary unavailability

## Additional Standards

### Logging & Observability
- Python `logging` with levels: DEBUG, INFO, WARNING, ERROR
- Default: WARNING (override with `-v/--verbose` or `--debug`)
- Formats: `text` (human-readable) or `json` (structured)
- JSON preferred when not attached to TTY
- Include request IDs and timestamps for correlation
- No telemetry without explicit opt-in

### Multimodal Support
**Content type detection**:
- Images (PNG, JPEG, GIF, WebP, BMP): sent as `image_url` if model supports vision
- PDFs: sent as `image_url` with PDF data URL if model supports PDF input
- Other binaries: text markers (e.g., `[Attached: application/zip, 1234 bytes]`)
- Capability detection: `litellm.utils.supports_vision()` and `supports_pdf_input()`
- Text input: UTF-8 with truncation notice if exceeds limit

### Streaming & Cost Tracking
- LiteLLM client sets `stream_options: { include_usage: true }`
- Enable cost tracking: `litellm.include_cost_in_streaming_usage = True`
- Read usage from final streamed chunk (`chunk.usage`)
- Do not rely on `_hidden_params["usage"]` when `include_usage` is enabled

### Token Budgeting
- Respect provider-specific limits with 20% safety margin
- Summarize when stdin/context exceeds budget
- Configure `max_tokens` and `temperature` per agent
- Handle 429/5xx with exponential backoff + jitter

## Development Workflow

### Commit Discipline
**Before every commit**:
1. Run linter: `UV_CACHE_DIR=.uv-cache uv run ruff check`
2. Run tests: `UV_CACHE_DIR=.uv-cache uv run pytest`
3. Update docs if API/behavior changed
4. Update memories for significant changes

**Branch Strategy**:
- `main`: stable, release-ready
- Feature branches: `feature/<name>`
- Fix branches: `fix/<issue>`

### Task Completion Checklist
- [ ] Lint clean: Ruff passes (or waivers documented)
- [ ] Tests pass: pytest succeeds
- [ ] Docs updated: README.md and AGENTS.md reflect changes
- [ ] Memories updated: relevant serena memories refreshed
- [ ] Config updated: pyproject.toml for tool config/deps
- [ ] Security: no secrets in logs or code; approval prompts TTY-safe
- [ ] Architecture: CLI entrypoint thin; complex logic in core modules
- [ ] Code quality: new code typed and async-safe

### Memory Management
**Persistent memories** (via serena tools):
- `project_overview`: architecture, modules, recent refactors
- `refactor_progress`: ongoing changes, rationale, next steps
- `task_completion_checklist`: goals per task with completion marks
- `style_and_conventions`: linting, typing, logging, code style
- `suggested_commands`: uv/lint/test commands

**Task-specific memories**: create for larger features/PRs; update at checkpoints.

### Planning
- Use `sequentialthinking` tool for multi-step planning
- Design trade-offs and verification before implementation
- Keep plans concise, actionable
- Verify outcomes before marking done

## Quality Gates

### Code Review Requirements
- All PRs must verify compliance with this constitution
- At least one approval before merge
- No merge if tests fail or linter reports errors
- Complexity must be justified in PR description

### Integration Testing Focus
**Required integration tests for**:
- New MCP server integrations
- Changes to tool discovery/invocation
- Session persistence/restoration
- CLI flag combinations
- Config merge behavior
- Multimodal content handling

### Performance Benchmarks
**Monitor and report**:
- CLI cold start time
- Config load time
- Tool discovery time per server
- First token latency (streaming)

**Performance regression gates**:
- 20% increase in startup time: investigate
- 50% increase: block merge

## Packaging & Dependencies

### uv/uvx Workflow (MANDATORY)
**All dev/test/install commands MUST use uv/uvx**
- Virtual env: `uv venv`
- Editable install: `UV_CACHE_DIR=.uv-cache uv pip install -e .[dev]`
- Run CLI: `UV_CACHE_DIR=.uv-cache uv run gptsh`
- Tests: `UV_CACHE_DIR=.uv-cache uv run pytest`
- Lint: `UV_CACHE_DIR=.uv-cache uv run ruff check`

**Sandbox**: Set `UV_CACHE_DIR=.uv-cache` to avoid home-directory permission issues.

### Dependency Policy
- Minimize dependencies; add only if essential
- Vet for security and maintainability
- Pin major versions; allow minor/patch updates
- Lock file optional: `uv lock` for reproducible builds

**Core dependencies**:
- litellm: LLM API abstraction
- mcp>=1.15.0: Model Context Protocol
- click: CLI framework
- rich: terminal UI/markdown
- pyyaml: config loading
- httpx: async HTTP
- python-dotenv: env var expansion
- gnureadline: readline for REPL

**Dev dependencies**:
- pytest: testing framework
- pytest-asyncio: async test support
- ruff: linter/formatter

## Governance

### Constitution Authority
- This constitution supersedes all other practices and documentation
- In case of conflict between constitution and code, constitution wins
- Amendments require:
  - Documentation of rationale
  - Approval from maintainers
  - Migration plan for affected code
- All PRs/reviews must verify compliance

### Complexity Justification
- Simple solutions preferred (YAGNI principle)
- Complexity must be justified in design docs
- Refactor to reduce complexity over time
- New abstractions require clear use case

### Runtime Guidance
- Use `AGENTS.md` for development instructions
- Agent-facing guidance in dedicated section
- Project reference for architecture facts
- Keep constitution focused on principles/standards

---

**Version**: 1.0.0 | **Ratified**: 2025-01-11 | **Last Amended**: 2025-01-11
