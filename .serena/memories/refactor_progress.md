# Refactor Progress (Post-Runner Consolidation)

- Introduced core/runner.run_turn as the unified execution path (streaming + tools fallback) used by both CLI and REPL.
- Added RunRequest dataclass to reduce parameter sprawl.
- Refactored REPL to reuse run_llm/run_turn; removed per-turn asyncio.run and loop thrash.
- Consolidated agent resolution and listings into cli/utils.py (resolve_agent_and_settings, print_tools_listing, print_agents_listing).
- Migrated domain/models to core/models; updated config_api and tests; removed domain/.
- Logging improvements: debug logs for tool-call deltas in streaming and in MCP client/tool resolver.
- CLI entrypoint thinned and uses helpers for both interactive and non-interactive flows.

Next:
- Consider accepting typed models in build_agent directly (AgentConfig/ProviderConfig).
- Optional: extract listing subcommands fully to utils for even thinner entrypoint.
- Optional: mid-stream tool execution (pause/resume) to remove fallback path.
