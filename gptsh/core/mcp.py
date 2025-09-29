import json
import os
import asyncio
from typing import Any, Dict, List, Optional
from gptsh.config.loader import _expand_env
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Discover tools from configured MCP servers using Model Context Protocol Python SDK.
    Runs discovery concurrently and isolates failures per server.
    """
    return asyncio.run(_list_tools_async(config))

async def _list_tools_async(config: Dict[str, Any]) -> Dict[str, List[str]]:
    # Load/merge MCP servers definitions from all configured files
    servers_files = config.get("mcp", {}).get("servers_files", [])
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r", encoding="utf-8") as f:
                raw = f.read()
            content = _expand_env(raw)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue

    # Determine a per-request timeout (fallback to a sensible default)
    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))

    async def _query_server(name: str, srv: Dict[str, Any]) -> List[str]:
        transport = srv.get("transport", {})
        ttype = transport.get("type")
        try:
            if ttype == "stdio":
                params = StdioServerParameters(
                    command=srv.get("command"),
                    args=srv.get("args", []),
                    env=srv.get("env", {}),
                )
                async def _stdio_call() -> List[str]:
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            resp = await session.list_tools()
                            return [tool.name for tool in resp.tools]
                return await asyncio.wait_for(_stdio_call(), timeout=timeout_seconds)

            elif ttype in ("http", "sse"):
                url = transport.get("url")
                headers = srv.get("credentials", {}).get("headers", {})
                async def _http_call() -> List[str]:
                    async with streamablehttp_client(url, headers=headers) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            resp = await session.list_tools()
                            return [tool.name for tool in resp.tools]
                return await asyncio.wait_for(_http_call(), timeout=timeout_seconds)

            else:
                # Unknown transport type, return empty tool list
                return []
        except Exception:
            # Any failure on a server should not crash the whole discovery
            return []

    # Run all server queries concurrently
    tasks = [asyncio.create_task(_query_server(name, srv)) for name, srv in servers.items()]
    results_list: List[List[str]] = []
    results_map: Dict[str, List[str]] = {}
    if tasks:
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for (name, _), res in zip(servers.items(), gathered):
            if isinstance(res, Exception):
                results_map[name] = []
            else:
                results_map[name] = res
    return results_map
