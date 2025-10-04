from __future__ import annotations

from typing import Any, Dict, List

from gptsh.interfaces import MCPClient
from gptsh.mcp import (
    ensure_sessions_started_async as _ensure_started,
    execute_tool_async as _execute_tool_async,
    list_tools as _list_tools,
)


class MCPManager(MCPClient):
    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await _ensure_started(self._config)
        self._started = True

    async def list_tools(self) -> Dict[str, List[str]]:
        return _list_tools(self._config)

    async def call_tool(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        return await _execute_tool_async(server, tool, args, self._config)

    async def stop(self) -> None:
        # Current implementation relies on event-loop lifecycle; nothing to stop explicitly.
        self._started = False

