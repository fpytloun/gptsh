# In-process stdio-like builtin MCP servers.
# Each module under gptsh.mcp.builtin.<name> must expose:
# - list_tools() -> list[str]
# - list_tools_detailed() -> list[dict]
# - execute(tool: str, arguments: dict) -> str
