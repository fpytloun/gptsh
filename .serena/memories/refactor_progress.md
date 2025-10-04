# Refactor Progress (2025-10-04)

## Completed
- Added interfaces: `LLMClient`, `MCPClient`, `ApprovalPolicy`, `ProgressReporter` (gptsh/interfaces.py)
- Implemented LiteLLM adapter: `LiteLLMClient` (gptsh/llm/litellm_client.py)
- Implemented MCP wrapper: `MCPManager` (gptsh/mcp/manager.py)
- Implemented approval policy: `DefaultApprovalPolicy` (gptsh/core/approval.py)
- Implemented progress reporter: `RichProgressReporter` (gptsh/core/progress.py)
- Tests added: adapters + policies + progress (gptsh/tests/test_interfaces_adapters.py)
- Tests passing: 4 passed via `UV_CACHE_DIR=.uv-cache uv run pytest -q`

## Notes
- Current CLI behavior unchanged; new components are additive and ready for orchestration wiring.

## Next
- Introduce `ChatSession` orchestrator in `gptsh/core/session.py` (move tool loop from `llm/session.py` to use interfaces)
- Wire CLI to instantiate `LiteLLMClient`, `MCPManager`, `DefaultApprovalPolicy`, `RichProgressReporter`, and use `ChatSession`
- Add domain models (`gptsh/domain/models.py`) and map config
- Extend tests for `ChatSession` flows and CLI listing
