# Task Completion Checklist (Updated)

Before marking a refactor/feature as complete:
- Lint: `UV_CACHE_DIR=.uv-cache uv run ruff check` is clean (or consciously waived).
- Tests: `UV_CACHE_DIR=.uv-cache uv run pytest` passes.
- Docs: README.md and AGENTS.md reflect any structural or workflow changes.
- Config: pyproject.toml updated for tool config (ruff) and optional dev deps.
- No secrets in logs or code; approval prompts are TTY-safe.
- Entry point remains thin; complex logic lives in core modules.
- New code is typed and async-safe.
