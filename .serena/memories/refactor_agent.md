# Implementation Plan: Agent-based Config Resolution

Goals
- Use LiteLLMClient as the only LLM provider (no extra abstraction).
- Keep gptsh.config for loading/merging config only.
- Move runtime types to gptsh.core (Agent, ToolHandle) and tool resolution to gptsh.mcp.
- ChatSession consumes an Agent instance (each agent = its own LiteLLMClient, tools, policy).

Proposed Structure
- core/agent.py
  - Agent dataclass: name: str; llm: LiteLLMClient; tools: list[ToolHandle] or dict[str, list[ToolHandle]]; policy: ApprovalPolicy; generation_params: dict.
- core/config_resolver.py
  - build_agent(config: dict, cli_overrides: dict) -> Agent
    - Resolve effective provider/agent via current helpers (or move helpers here).
    - Instantiate LiteLLMClient(base_params={model + gen params}).
    - Resolve tools via mcp/tools_resolver (filter by agent.tools/CLI allowed list).
    - Build DefaultApprovalPolicy from config/agent approvals.
- mcp/tools_resolver.py
  - resolve_tools(config, allowed_servers: list[str] | None) -> list[ToolHandle]
    - Ensure MCP sessions started (do not mutate global config allowed_servers).
    - Discover tools; filter by allowed servers.
    - Build ToolHandle(server, name, description, input_schema, invoke=execute_tool_async).

LiteLLMClient enhancement
- Add base_params: dict[str, Any] on init. Merge with call params in complete/stream.
- ChatSession uses agent.llm and agent.policy.

CLI changes (minimal)
- Build Agent via core.config_resolver.build_agent(config, cli_overrides).
- Pass Agent to ChatSession; use agent.tools for listing.
- Keep spinner UX as currently fixed.

Testing
- Resolver tests: build_agent resolves provider/agent precedence and returns Agent with llm base_params.
- Tools resolver tests: returns ToolHandles filtered by allowed servers and invokes MCP correctly.
- ChatSession tests: accepts Agent and performs LLM/tool flow using agent.llm/agent.policy.
