from .client import (
    _discover_tools_detailed_async,
    _execute_tool_async,
    discover_tools_detailed,
    ensure_sessions_started_async as _ensure_sessions_started_async,
    execute_tool,
    get_auto_approved_tools,
    list_tools,
    stop_all_sessions_async as _stop_all_sessions_async,
)


async def discover_tools_detailed_async(config):
    return await _discover_tools_detailed_async(config)

async def execute_tool_async(server, tool, arguments, config):
    return await _execute_tool_async(server, tool, arguments, config)

async def ensure_sessions_started_async(config):
    return await _ensure_sessions_started_async(config)

async def stop_all_sessions():
    return await _stop_all_sessions_async()

__all__ = [
    "list_tools",
    "get_auto_approved_tools",
    "discover_tools_detailed",
    "execute_tool",
    "discover_tools_detailed_async",
    "execute_tool_async",
    "ensure_sessions_started_async",
    "stop_all_sessions",
]
