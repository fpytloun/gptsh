# Style and Conventions (Post-Runner Refactor)

- Linting: Ruff is the primary linter (configured in pyproject, line-length=100, isort on). Always run `ruff check` before committing.
- Imports: group stdlib / third-party / first-party (ruff I001).
- Line length: 100 for code, comments, decorator lines.
- Async-first: any blocking work (HTTP, MCP, subprocess, file IO) must be async.
- Types: full typing; prefer `from __future__ import annotations`.
- Errors/Exit codes: 0,1,4,124,130; raise ToolApprovalDenied when required tools denied.
- Logging: use `core.logging.setup_logging`; never log secrets; redact keys later.
- CLI: keep entrypoint thin—delegate to core.runner/core.api and REPL helpers.
- Tests: pytest/pytest-asyncio; add unit tests near component domains; avoid network.
- Config: use `core.models` + `core.config_api` for selection; avoid raw dict handling in CLI; use `cli/utils.resolve_agent_and_settings` in CLI.
- MCP: use `mcp.api` / `MCPManager`; builtins in `mcp/builtin`.

## Commands to run before commit
- `UV_CACHE_DIR=.uv-cache uv run ruff check`
- `UV_CACHE_DIR=.uv-cache uv run pytest`
- Optional: `UV_CACHE_DIR=.uv-cache uv run ruff check --fix`