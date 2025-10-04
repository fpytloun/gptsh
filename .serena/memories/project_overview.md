# Project Overview (Post-Refactor)

- Core architecture
  - Interfaces: LLMClient, MCPClient, ApprovalPolicy, ProgressReporter
  - Adapters: LiteLLMClient; MCPManager (+ mcp/api facade)
  - Orchestrator: ChatSession (tool loop + streaming helpers)
  - Config/domain: domain/models; core/config_api for effective selection and tool policy
  - CLI: thin entrypoint fueled by core APIs; REPL helpers in core/repl with command registry (/help, /agent, /model, /reasoning_effort)
  - Builtins: mcp/builtin (time, shell)

- Tooling
  - Ruff is the primary linter (pyproject configured; line length 100; isort enabled)
  - Pytest is the unit test runner (pytest-asyncio for async)
  - Use `UV_CACHE_DIR=.uv-cache` for uv commands in sandboxed environments

- Commands
  - Lint: `UV_CACHE_DIR=.uv-cache uv run ruff check`
  - Tests: `UV_CACHE_DIR=.uv-cache uv run pytest`
  - CLI: `UV_CACHE_DIR=.uv-cache uv run gptsh --help`

- Exit codes and approvals
  - 0 success; 1 generic failure; 4 tool approval denied; 124 timeout; 130 interrupt
  - DefaultApprovalPolicy auto-denies in non-TTY and prompts otherwise

- Next Iterations
  - Logging redaction utility; deeper MCP lifecycle integration tests
