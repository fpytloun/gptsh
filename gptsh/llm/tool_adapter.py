from typing import Any, Dict, List
from gptsh.mcp import discover_tools_detailed_async

async def build_llm_tools(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build OpenAI-style tool specs from MCP tool discovery.
    Tool names are prefixed with '<server>__' to route calls back.
    """
    tools: List[Dict[str, Any]] = []
    detailed = await discover_tools_detailed_async(config)
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
