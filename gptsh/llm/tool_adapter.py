import logging
from typing import Any, Dict, List

from gptsh.mcp import discover_tools_detailed_async

logger = logging.getLogger(__name__)


async def build_llm_tools(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build OpenAI-style tool specs from MCP tool discovery.
    Tool names are prefixed with '<server>__' to route calls back.
    """
    tools: List[Dict[str, Any]] = []
    logger.debug("Building LLM tools from discovery")
    detailed = await discover_tools_detailed_async(config)
    logger.debug("Discovery servers: %s", list((detailed or {}).keys()))
    for server, items in detailed.items():
        for t in items:
            name = f"{server}__{t['name']}"
            description = t.get("description") or ""
            params = t.get("input_schema") or {"type": "object", "properties": {}, "additionalProperties": True}
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            })
    return tools

def build_llm_tools_from_handles(tools_map: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """
    Build OpenAI-style tool specs from resolved ToolHandle objects.
    The handle must expose: name, description, input_schema. Grouping key is the server label.
    """
    specs: List[Dict[str, Any]] = []
    for server, handles in (tools_map or {}).items():
        for h in handles:
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{server}__{getattr(h, 'name', '')}",
                        "description": getattr(h, "description", "") or "",
                        "parameters": getattr(h, "input_schema", None) or {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    },
                }
            )
    return specs

def parse_tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract tool_calls from a LiteLLM-normalized response.
    """
    calls: List[Dict[str, Any]] = []
    try:
        choice0 = (resp.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        tcalls = msg.get("tool_calls") or []
        # Normalize
        for c in tcalls:
            f = c.get("function") or {}
            name = f.get("name")
            arguments = f.get("arguments")
            call_id = c.get("id")
            if name:
                calls.append({"id": call_id, "name": name, "arguments": arguments})
    except Exception:
        pass
    return calls
