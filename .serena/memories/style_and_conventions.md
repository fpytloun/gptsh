# Style and Conventions (Updated for Ruff/Pytest)

- Linting: Ruff is the primary linter (configured in pyproject, line-length=100, isort on). Always run `ruff check` before committing.
- Imports: group stdlib / third-party / first-party and keep import blocks sorted (ruff I001).
- Line length: 100 for code, comments, decorator lines; wrap long strings/docs.
- Async-first: any blocking work (HTTP, MCP, subprocess, file IO) must be async.
- Types: full typing; prefer `from __future__ import annotations` in modules.
- Errors/Exit codes: map to 0,1,4,124,130; raise ToolApprovalDenied for required tools.
- Logging: use `core.logging.setup_logging`; never log secrets/headers; redaction utility planned.
- CLI: keep entrypoint thin—delegate to `core.api`/`core.session` and REPL helpers.
- Tests: pytest/pytest-asyncio; add unit tests near component domains; do not rely on network.
- Config: use `domain.models` + `core.config_api` for effective selection; avoid raw dict handling in CLI.
- MCP: use `mcp.api` and `MCPManager`; builtin tools via `mcp/builtin`.

## Commands to run before commit
- `UV_CACHE_DIR=.uv-cache uv run ruff check`
- `UV_CACHE_DIR=.uv-cache uv run pytest`
- Optional: `UV_CACHE_DIR=.uv-cache uv run ruff check --fix`