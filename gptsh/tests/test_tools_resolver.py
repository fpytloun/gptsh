from typing import Any, Dict

import pytest

from gptsh.mcp.tools_resolver import resolve_tools


@pytest.mark.asyncio
async def test_tools_resolver_filters_and_invokes(monkeypatch):
    # Fake MCP discovery returning tools across servers
    async def fake_discover(config: Dict[str, Any]):
        return {
            "fs": [
                {"name": "read", "description": "", "input_schema": {}},
                {"name": "write", "description": "", "input_schema": {}},
            ],
            "time": [
                {"name": "now", "description": "", "input_schema": {}},
            ],
        }

    async def fake_ensure(_config: Dict[str, Any]):
        return None

    async def fake_execute(server: str, name: str, args: Dict[str, Any], _config: Dict[str, Any]):
        return f"{server}:{name}:{args.get('x','')}"

    monkeypatch.setattr("gptsh.mcp.tools_resolver.discover_tools_detailed_async", fake_discover)
    monkeypatch.setattr("gptsh.mcp.tools_resolver.ensure_sessions_started_async", fake_ensure)
    monkeypatch.setattr("gptsh.mcp.tools_resolver.execute_tool_async", fake_execute)

    tools = await resolve_tools(config={}, allowed_servers=["fs"])  # filter to fs only
    assert set(tools.keys()) == {"fs"}
    handles = tools["fs"]
    names = {h.name for h in handles}
    assert names == {"read", "write"}

    # Invoke one handle and verify it goes through execute_tool_async
    read_handle = next(h for h in handles if h.name == "read")
    out = await read_handle.invoke({"x": 1})
    assert out == "fs:read:1"


@pytest.mark.asyncio
async def test_builtin_servers_are_merged_even_with_inline_servers(monkeypatch):
    # Ensure that built-ins like 'time' are present even if inline servers provided
    from gptsh.mcp.builtin import get_builtin_servers
    builtins = get_builtin_servers()
    assert "time" in builtins and "shell" in builtins

    async def fake_discover(config: Dict[str, Any]):
        # Discovery should include builtin 'time' even if inline only lists 'custom'
        return {"custom": [{"name": "do", "description": "", "input_schema": {}}], "time": [{"name": "now", "description": "", "input_schema": {}}]}

    async def fake_ensure(_config: Dict[str, Any]):
        return None

    async def fake_execute(server: str, name: str, args: Dict[str, Any], _config: Dict[str, Any]):
        return f"{server}:{name}"

    monkeypatch.setattr("gptsh.mcp.tools_resolver.discover_tools_detailed_async", fake_discover)
    monkeypatch.setattr("gptsh.mcp.tools_resolver.ensure_sessions_started_async", fake_ensure)
    monkeypatch.setattr("gptsh.mcp.tools_resolver.execute_tool_async", fake_execute)

    cfg = {"mcp": {"servers": {"custom": {"transport": {"type": "stdio"}, "command": "echo"}}}}
    tools = await resolve_tools(config=cfg)
    # Both custom and time should be present
    assert set(tools.keys()) >= {"custom", "time"}
