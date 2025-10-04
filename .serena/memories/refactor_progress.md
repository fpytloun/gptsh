# Refactor Progress (2025-10-04)

## Completed
- Removed legacy `gptsh/llm/session.py`; unified streaming via ChatSession + chunk_utils
- Cleaned CLI comments and imports accordingly
- Test suite remains green (14/14)

## Remaining suggestions
- Add `core/api.py` as high-level orchestration entry for potential future frontends
- Introduce `mcp/api.py` facade to centralize discovery/approval helpers
- Normalize logging redaction based on config `logging.redact_keys`
