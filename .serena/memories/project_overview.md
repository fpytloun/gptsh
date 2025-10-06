# Project Overview (Post-Runner Refactor)

- Core architecture
  - Interfaces: LLMClient, MCPClient, ApprovalPolicy, ProgressReporter
  - Adapters: LiteLLMClient; MCPManager (+ mcp/api facade)
  - Orchestrator: ChatSession (tool loop + params builder)
  - Runner: core/runner.run_turn — unified execution (streaming + tools fallback)
  - Config models: core/models (ProviderConfig, AgentConfig, AgentPrompt, Defaults)
  - Config helpers: core/config_api (select agent/provider, tools policy, output)
  - CLI: thin entrypoint and cli/utils (agent resolution + listings)
  - REPL: core/repl uses runner for turns
  - Builtins: mcp/builtin (time, shell)

- Tooling
  - Ruff (line length 100, isort enabled)
  - Pytest (pytest-asyncio)
  - Prefer `UV_CACHE_DIR=.uv-cache` for uv commands

- Commands
  - Lint: `UV_CACHE_DIR=.uv-cache uv run ruff check`
  - Tests: `UV_CACHE_DIR=.uv-cache uv run pytest`
  - CLI: `UV_CACHE_DIR=.uv-cache uv run gptsh --help`

- Exit codes & approvals
  - 0 ok; 1 generic; 4 tool approval denied; 124 timeout; 130 interrupt
  - DefaultApprovalPolicy auto-deny in non-TTY and prompt otherwise

- Notes
  - Tools are available in both streaming and non-streaming modes; runner falls back to non-stream if model streams only tool_calls.
  - domain/models removed; models live in core/models.