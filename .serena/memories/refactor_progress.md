# Refactor Progress (2025-10-04)

## Summary of Changes (Thorough)

Architecture and Core Abstractions
- Added Interfaces: `LLMClient`, `MCPClient`, `ApprovalPolicy`, `ProgressReporter` (gptsh/interfaces.py). These decouple orchestrator/CLI from concrete implementations.
- LLM Adapter: Implemented `LiteLLMClient` (gptsh/llm/litellm_client.py) to standardize non-stream and stream access via litellm.
- MCP Wrapper: Implemented `MCPManager` (gptsh/mcp/manager.py) as the `MCPClient` bridge to existing MCP session logic.
- Approval Policy: Implemented `DefaultApprovalPolicy` (gptsh/core/approval.py), including wildcard logic and TTY-aware user prompts (auto-deny when non-interactive).
- Progress Abstraction: Implemented `RichProgressReporter` (gptsh/core/progress.py). CLI is now wired to this instead of raw Rich.
- Orchestrator: Implemented `ChatSession` (gptsh/core/session.py) to own tool loops, approvals, and param building. Added streaming helpers: `prepare_stream`, `stream_with_params`.

Configuration and Domain Models
- Domain Models: Added `ProviderConfig`, `AgentConfig`, `AgentPrompt`, `Defaults` and helpers `map_config_to_models`, `pick_effective_agent_provider` (gptsh/domain/models.py). CLI uses these for agent/provider selection.
- Config Helpers: Added `select_agent_provider_dicts`, `effective_output`, `compute_tools_policy` (gptsh/core/config_api.py) so the CLI resolves effective output and tools policy via a single API.

CLI Wiring and Cleanup
- CLI uses `ChatSession` for tool flows and `RichProgressReporter` everywhere for progress.
- Streaming path uses `ChatSession.prepare_stream()`/`stream_with_params()` (kept monkeypatch-friendly in tests).
- Non-stream tool/no-tool path unified under a high-level API: `run_prompt()` (gptsh/core/api.py). A streaming param prep helper `prepare_stream_params()` exists (for potential future use).
- MCP facade: Added `gptsh/mcp/api.py` to wrap `list_tools`, `discover_tools_detailed`, and `get_auto_approved_tools`.
- REPL refactor: Extracted REPL logic into `gptsh/core/repl.py`:
  - `build_prompt`, `setup_readline`, `add_history`.
  - Slash-command handlers `command_exit`, `command_model`, `command_reasoning_effort`, `command_agent`.
  - Command registry and help: `get_command_names`, `command_help`.
  - Entry’s REPL now routes slash-commands via these helpers.
- Removed legacy duplication: Deleted `gptsh/llm/session.py` (migrated logic) and centralized stream chunk parsing in `gptsh/llm/chunk_utils.py:extract_text`.

Error Handling and Exit Codes
- Added `ToolApprovalDenied` (gptsh/core/exceptions.py). `ChatSession` raises when tool_choice is required and user denies.
- CLI maps to exit codes: 4 (denied), 124 (timeout), 130 (interrupt), 1 (generic failure). Tests cover 4 and 124.

Code Quality and Lint
- Cleaned unused imports and clarified annotations:
  - CLI import order updated; moved warnings.filterwarnings() below imports to satisfy Ruff E402.
  - Added `# noqa: F401` where imports are intentionally indirect (mcp/client.py: httpx).
  - Reintroduced explicit `LiteLLMClient` in streaming branch to avoid passing None.
- Minor inline comments for clarity and maintainability.

Tests (with uv)
- Unit tests for adapters/policies/progress: gptsh/tests/test_interfaces_adapters.py.
- ChatSession orchestration: gptsh/tests/test_chat_session.py (auto-approve, deny, multi-tools).
- Domain model mapping/select: gptsh/tests/test_domain_models.py.
- Core API seams: gptsh/tests/test_core_api.py.
- CLI coverage: gptsh/tests/test_cli_entrypoint.py (list-tools, agent/provider selection, streaming without tools, approval denied exit 4, timeout exit 124, list-agents structure).
- Config helpers: gptsh/tests/test_config_api.py.
- REPL helpers and registry: gptsh/tests/test_repl.py.
- Current suite: 25 tests passing, with two benign warnings (3p deprecation + dummy async stop in test). All changes validated.

## Current State
- Modular architecture in place: interfaces, adapters, orchestrator, progress, approvals, domain models.
- CLI refactored to rely on core abstractions and helpers; REPL is cleaner and extensible with a command registry and help.
- Legacy code removed; duplication minimized.
- Lint compliance improved (Ruff E402 addressed; small noqa kept where appropriate).

## Next Steps
- Logging Redaction (deferred/requested):
  - Add a shared redaction utility to honor `config.logging.redact_keys` across CLI and `ChatSession` logging. Ensure we never log secrets or headers by mistake.
- MCP Lifecycle Integration Tests (future):
  - Add an integration test suite to spawn mock stdio/SSE servers, exercise reconnect/backoff, and chaos scenarios. Validate MCP manager resilience per REFACTOR.md.
- Additional REPL Improvements (optional):
  - Consider extracting stdin/TTY reattach handling into a tiny helper to further thin the entrypoint.
  - Extend command registry to support dynamic addition (plugins) if needed later.
- CLI Snapshot Tests (optional):
  - Add snapshot-like tests for list-agents and list-tools to lock UX, including badges for approved tools.
- Code Style/Lint:
  - Run `ruff` locally and address any new suggestions (beyond E402). Adopt consistency rules for import grouping if desired.
- Documentation:
  - Update README/REFACTOR.md to reflect the completed work: ChatSession orchestration, progress abstraction usage, config helpers, MCP/LLM adapters, REPL commands.
