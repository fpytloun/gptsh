# Refactor Progress (2025-10-04)

## Completed
- Carved REPL command handling into `gptsh/core/repl.py` with small, testable helpers:
  - `build_prompt`, `command_exit`, `command_model`, `command_reasoning_effort`, `command_agent`
- Updated entrypoint REPL to use these helpers; reduced duplication and improved clarity
- All tests remain green (19/19)

## Next
- Consider adding unit tests for `gptsh/core/repl.py` helpers
- Logging redaction (later)
