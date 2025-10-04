# Refactor Progress (2025-10-04)

## Completed (since last update)
- Introduced `ToolApprovalDenied` exception and mapped to CLI exit code 4 when tool_choice is required
- Added CLI tests: list-agents output and approval denied exit code
- Unified streaming via ChatSession helpers
- Test suite: 13 passing

## Current State
- CLI: list-tools, list-agents, streaming, tool flows all covered
- Domain models integrated for selection, approvals safe in non-tty

## Next
- Optional: refine list-agents output to use domain models for consistency
- Add timeout/interrupt handling tests mapping to exit codes (124/130) if feasible in unit tests
- Review remaining items in REFACTOR.md for additional refactors (e.g., progress reporter abstraction in CLI)
