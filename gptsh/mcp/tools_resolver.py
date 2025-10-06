from __future__ import annotations

from typing import Any, Dict, List, Optional

from gptsh.core.agent import ToolHandle
from gptsh.mcp import (
    discover_tools_detailed_async,
    ensure_sessions_started_async,
    execute_tool_async,
)


import logging
logger = logging.getLogger(__name__)


async def resolve_tools(config: Dict[str, Any], allowed_servers: Optional[List[str]] = None) -> Dict[str, List[ToolHandle]]:
    # Ensure MCP sessions started; do not mutate global allowed servers here
    await ensure_sessions_started_async(config)

    tools_map: Dict[str, List[ToolHandle]] = {}
    logger.debug("Resolving tools (allowed=%s)", allowed_servers)
    detailed = await discover_tools_detailed_async(config)
    logger.debug("Discovered tools detail servers=%s", list((detailed or {}).keys()))
    for server, items in (detailed or {}).items():
        if allowed_servers is not None and server not in allowed_servers:
            continue
        out: List[ToolHandle] = []
        for t in items:
            name = str(t.get("name"))
            desc = t.get("description") or ""
            schema = t.get("input_schema") or {"type": "object", "properties": {}, "additionalProperties": True}

            async def _executor(s: str, n: str, args: Dict[str, Any]) -> str:
                logger.debug("ToolHandle executor: %s.%s args_keys=%s", s, n, list((args or {}).keys()))
                return await execute_tool_async(s, n, args, config)

            out.append(ToolHandle(server=server, name=name, description=desc, input_schema=schema, _executor=_executor))
        tools_map[server] = out
    return tools_map
