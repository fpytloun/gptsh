# Refactor Progress (2025-10-04)

## Completed
- Refactored CLI to use config helpers: effective output resolution and tools policy combination
- Removed redundant inline logic and prepared tools filter once from CLI args
- All tests remain green: 19/19

## Next
- Implement logging redaction helper and apply
- Optionally move remaining MCP list-agents display logic to use domain models for stricter consistency
