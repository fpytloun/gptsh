import json
import os
import sys
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from gptsh.config.loader import _expand_env
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
import httpx

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Discover tools from configured MCP servers using Model Context Protocol Python SDK.
    Runs discovery concurrently and isolates failures per server.
    """
    return asyncio.run(_list_tools_async(config))

async def _list_tools_async(config: Dict[str, Any]) -> Dict[str, List[str]]:
    # Load/merge MCP servers definitions from all configured files
    # Resolve list of MCP servers files with precedence:
    # 1) CLI-provided (already stored under mcp.servers_files by CLI)
    # 2) Config key 'mcp.servers_files' or legacy 'mcp.mcp_servers'
    # 3) Defaults
    mcp_conf = config.get("mcp", {}) or {}
    servers_files = mcp_conf.get("servers_files")
    if not servers_files:
        servers_files = mcp_conf.get("mcp_servers")
    if isinstance(servers_files, str):
        servers_files = [servers_files]
    if not servers_files:
        servers_files = [
            os.path.expanduser("~/.config/gptsh/mcp_servers.json"),
            os.path.abspath("./.gptsh/mcp_servers.json"),
        ]
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r", encoding="utf-8") as f:
                raw = f.read()
            # Normalize ${env:VAR} -> ${VAR} first, then expand using existing _expand_env
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue

    # Determine a per-request timeout (fallback to a sensible default)
    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))

    async def _query_server(name: str, srv: Dict[str, Any]) -> List[str]:
        transport = srv.get("transport", {})
        ttype = transport.get("type")
        if not ttype:
            if transport.get("url") or srv.get("url"):
                ttype = "http"
            elif srv.get("command"):
                ttype = "stdio"
            else:
                ttype = None
        try:
            if ttype == "stdio":
                if not srv.get("command"):
                    logging.getLogger(__name__).warning("MCP server '%s' uses stdio but has no 'command' configured", name)
                    return []
                params = StdioServerParameters(
                    command=srv.get("command"),
                    args=srv.get("args", []),
                    env=srv.get("env", {}),
                )
                async def _stdio_call() -> List[str]:
                    async with stdio_client(params, errlog=sys.stderr if logging.getLogger(__name__).getEffectiveLevel() <= logging.DEBUG else asyncio.subprocess.DEVNULL) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            resp = await session.list_tools()
                            return [tool.name for tool in resp.tools]
                return await asyncio.wait_for(_stdio_call(), timeout=timeout_seconds)

            elif ttype in ("http", "sse"):
                url = transport.get("url") or srv.get("url")
                if not url:
                    logging.getLogger(__name__).warning("MCP server '%s' missing transport.url/url for '%s' transport", name, ttype)
                    return []
                headers = (
                    srv.get("credentials", {}).get("headers")
                    or transport.get("headers")
                    or srv.get("headers")
                    or {}
                )
                async def _http_call() -> List[str]:
                    async def via_streamable() -> List[str]:
                        async with streamablehttp_client(url, headers=headers) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                resp = await session.list_tools()
                                return [tool.name for tool in resp.tools]

                    async def via_sse() -> List[str]:
                        async with sse_client(url, headers=headers) as (read, write):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                resp = await session.list_tools()
                                return [tool.name for tool in resp.tools]

                    # Heuristic: URLs containing '/sse' use SSE; otherwise try streamable HTTP first,
                    # and fall back to SSE on typical "method not allowed/not found/bad request" errors.
                    if re.search(r"/sse(?:$|[/?])", url):
                        return await via_sse()
                    try:
                        return await via_streamable()
                    except httpx.HTTPStatusError as e:
                        code = getattr(getattr(e, "response", None), "status_code", None)
                        if code in (400, 404, 405):
                            logging.getLogger(__name__).info("HTTP %s from %s; retrying with SSE", code, url)
                            return await via_sse()
                        raise
                return await asyncio.wait_for(_http_call(), timeout=timeout_seconds)

            else:
                # Unknown transport type, return empty tool list
                logging.getLogger(__name__).warning("MCP server '%s' has unknown transport type: %r", name, ttype)
                return []
        except Exception as e:
            # Any failure on a server should not crash the whole discovery
            logging.getLogger(__name__).warning("MCP tool discovery failed for server '%s': %s", name, e, exc_info=True)
            return []

    # Run all server queries concurrently, honoring 'disabled' servers
    results_map: Dict[str, List[str]] = {}
    tasks: List[asyncio.Task] = []
    task_names: List[str] = []
    for name, srv in servers.items():
        if srv.get("disabled"):
            # Mark disabled servers with empty tool list and skip querying
            results_map[name] = []
            continue
        tasks.append(asyncio.create_task(_query_server(name, srv)))
        task_names.append(name)

    if tasks:
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for name, res in zip(task_names, gathered):
            if isinstance(res, Exception):
                results_map[name] = []
            else:
                results_map[name] = res
    return results_map

async def _open_session(name: str, srv: Dict[str, Any], timeout_seconds: float):
    """
    Async context manager yielding an initialized ClientSession for given server.
    Detects transport (stdio/http/sse) and opens appropriate client.
    """
    transport = srv.get("transport", {})
    ttype = transport.get("type")
    if not ttype:
        if transport.get("url") or srv.get("url"):
            ttype = "http"
        elif srv.get("command"):
            ttype = "stdio"
        else:
            ttype = None

    if ttype == "stdio":
        if not srv.get("command"):
            raise RuntimeError(f"MCP server '{name}' uses stdio but has no 'command'")
        params = StdioServerParameters(
            command=srv.get("command"),
            args=srv.get("args", []),
            env=srv.get("env", {}),
        )
        cm = stdio_client(params, errlog=sys.stderr if logging.getLogger(__name__).getEffectiveLevel() <= logging.DEBUG else asyncio.subprocess.DEVNULL)
        async def _ctx():
            async with cm as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return _ctx()

    elif ttype in ("http", "sse"):
        url = transport.get("url") or srv.get("url")
        if not url:
            raise RuntimeError(f"MCP server '{name}' missing transport.url/url for '{ttype}' transport")
        headers = (
            srv.get("credentials", {}).get("headers")
            or transport.get("headers")
            or srv.get("headers")
            or {}
        )

        async def _ctx_streamable():
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        async def _ctx_sse():
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        # Heuristic selection between streamable_http and sse, with fallback on typical errors
        if re.search(r"/sse(?:$|[/?])", url):
            return _ctx_sse()
        try:
            # Probe with streamable by opening and closing quickly to validate
            return _ctx_streamable()
        except Exception:
            return _ctx_sse()
    else:
        raise RuntimeError(f"MCP server '{name}' has unknown transport type: {ttype!r}")

def discover_tools_detailed(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Return detailed MCP tool definitions per server:
      { server_name: [ {name, description, input_schema}, ... ] }
    """
    return asyncio.run(_discover_tools_detailed_async(config))

async def _discover_tools_detailed_async(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    # Reuse servers loading logic
    mcp_conf = config.get("mcp", {}) or {}
    servers_files = mcp_conf.get("servers_files")
    if not servers_files:
        servers_files = mcp_conf.get("mcp_servers")
    if isinstance(servers_files, str):
        servers_files = [servers_files]
    if not servers_files:
        servers_files = [
            os.path.expanduser("~/.config/gptsh/mcp_servers.json"),
            os.path.abspath("./.gptsh/mcp_servers.json"),
        ]
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue

    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
    results: Dict[str, List[Dict[str, Any]]] = {}

    async def _per_server(name: str, srv: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        if srv.get("disabled"):
            return name, []
        try:
            async with _open_session(name, srv, timeout_seconds) as session:  # type: ignore[attr-defined]
                resp = await session.list_tools()
                out: List[Dict[str, Any]] = []
                for tool in resp.tools:
                    # tool.inputSchema may be None; default to open object
                    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}, "additionalProperties": True}
                    desc = getattr(tool, "description", None) or ""
                    out.append({
                        "name": tool.name,
                        "description": desc,
                        "input_schema": schema,
                    })
                return name, out
        except Exception as e:
            logging.getLogger(__name__).warning("MCP detailed tool discovery failed for '%s': %s", name, e, exc_info=True)
            return name, []

    tasks = [asyncio.create_task(_per_server(n, s)) for n, s in servers.items()]
    if tasks:
        pairs = await asyncio.gather(*tasks, return_exceptions=False)
        for name, tools in pairs:
            results[name] = tools
    return results

def execute_tool(server: str, tool: str, arguments: Dict[str, Any], config: Dict[str, Any]) -> str:
    """
    Execute a single MCP tool call and return concatenated string content result.
    """
    return asyncio.run(_execute_tool_async(server, tool, arguments, config))

async def _execute_tool_async(server: str, tool: str, arguments: Dict[str, Any], config: Dict[str, Any]) -> str:
    # Load servers
    mcp_conf = config.get("mcp", {}) or {}
    servers_files = mcp_conf.get("servers_files")
    if not servers_files:
        servers_files = mcp_conf.get("mcp_servers")
    if isinstance(servers_files, str):
        servers_files = [servers_files]
    if not servers_files:
        servers_files = [
            os.path.expanduser("~/.config/gptsh/mcp_servers.json"),
            os.path.abspath("./.gptsh/mcp_servers.json"),
        ]
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue
    if server not in servers:
        raise RuntimeError(f"MCP server '{server}' not configured")

    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
    srv = servers[server]
    try:
        async with _open_session(server, srv, timeout_seconds) as session:  # type: ignore[attr-defined]
            resp = await session.call_tool(tool, arguments or {})
            # resp.content is a list of content items; join text items
            texts: List[str] = []
            for item in getattr(resp, "content", []) or []:
                # Support multiple content types; prefer text
                t = getattr(item, "text", None)
                if t is not None:
                    texts.append(str(t))
                else:
                    # Fallback to any stringifiable representation
                    try:
                        texts.append(str(item))
                    except Exception:
                        pass
            return "\n".join(texts).strip()
    except Exception as e:
        logging.getLogger(__name__).warning("MCP tool execution failed for %s:%s: %s", server, tool, e, exc_info=True)
        raise

def get_auto_approved_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Load per-server autoApprove tool lists from configured MCP servers files.
    Returns mapping: server_name -> list of tool names to auto-approve.
    Disabled servers are still included if present in config so the UI can display badges,
    but they will typically have no discovered tools.
    """
    servers_files = config.get("mcp", {}).get("servers_files", [])
    servers: Dict[str, Any] = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue
        except Exception:
            # If parse fails for a file, skip it
            continue

    approved_map: Dict[str, List[str]] = {}
    for name, srv in servers.items():
        tools = srv.get("autoApprove") or []
        # Normalize to list[str]
        if isinstance(tools, list):
            approved_map[name] = [str(t) for t in tools]
        elif isinstance(tools, str):
            approved_map[name] = [tools]
        else:
            approved_map[name] = []
    return approved_map
