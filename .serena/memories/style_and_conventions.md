# Style and Conventions

- Async-first: Use `asyncio` for any blocking I/O; prefer `httpx.AsyncClient`; async subprocess for stdio.
- Typing: Prefer full type hints across modules; return precise types (e.g., `Dict[str, Any]`, `Optional[...]`).
- Config: Merge order: global main, global snippets (`~/.config/gptsh/config.d/*.yml`), project local; env expansion `${VAR}`; support custom `!include` tag.
- Logging: Python `logging` with config-driven levels/format (text/json). Never log secrets; redact sensitive values; include timestamps and request IDs when applicable.
- CLI: `click` for options/args; follow help text patterns present in `main()`; error with `click.BadParameter`/`click.ClickException` for user errors.
- MCP: Resilient lifecycle; auto-reconnect; healthcheck via `initialize`/`list_tools`; approvals before destructive actions.
- Security: Secrets only via env vars; no hardcoded keys; least privilege for subprocesses/I/O.
- Output: Support `--output [text|markdown]`; stream when possible; progress spinners to stderr; avoid printing secrets.
- Tests: Use `pytest` and `pytest-asyncio`; mock `litellm` calls in tests like `DummyCompletion` pattern.
