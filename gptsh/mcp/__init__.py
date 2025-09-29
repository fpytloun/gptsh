from .client import (
    list_tools,
    get_auto_approved_tools,
    discover_tools_detailed,
    execute_tool,
    _discover_tools_detailed_async,
    _execute_tool_async,
)

async def discover_tools_detailed_async(config):
    return await _discover_tools_detailed_async(config)

async def execute_tool_async(server, tool, arguments, config):
    return await _execute_tool_async(server, tool, arguments, config)

__all__ = [
    "list_tools",
    "get_auto_approved_tools",
    "discover_tools_detailed",
    "execute_tool",
    "discover_tools_detailed_async",
    "execute_tool_async",
]
