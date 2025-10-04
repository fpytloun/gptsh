# Refactor Progress (2025-10-04)

## Completed
- Added core API (`gptsh/core/api.py`) and MCP facade (`gptsh/mcp/api.py`)
- Refactored CLI to use core API for non-stream (run_prompt) and to build stream params via ChatSession
- Kept streaming path monkeypatchable for tests
- Added tests for core API seams (gptsh/tests/test_core_api.py)
- Fixed and stabilized CLI tests; now 16 tests passing

## Next Suggestions
- Normalize logging redaction using config keys
- Add config API helpers to consolidate effective option resolution
- Optional: write integration tests for MCP lifecycle in a separate suite
