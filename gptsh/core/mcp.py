import json
import os
import asyncio
from typing import Any, Dict, List
from gptsh.config.loader import _expand_env
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Discover tools from configured MCP servers using Model Context Protocol Python SDK.
    """
    return asyncio.run(_list_tools_async(config))

async def _list_tools_async(config: Dict[str, Any]) -> Dict[str, List[str]]:
    servers_files = config.get("mcp", {}).get("servers_files", [])
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            raw = open(expanded).read()
            content = _expand_env(raw)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue

    results: Dict[str, List[str]] = {}
    for name, srv in servers.items():
        transport = srv.get("transport", {})
        if transport.get("type") == "stdio":
            params = StdioServerParameters(
                command=srv.get("command"),
                args=srv.get("args", []),
                env=srv.get("env", {}),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    resp = await session.list_tools()
                    results[name] = [tool.name for tool in resp.tools]
        elif transport.get("type") in ("http", "sse"):
            url = transport.get("url")
            headers = srv.get("credentials", {}).get("headers", {})
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    resp = await session.list_tools()
                    results[name] = [tool.name for tool in resp.tools]
        else:
            results[name] = []
    return results
