import json
import os
import sys
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager
from gptsh.config.loader import _expand_env
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
import httpx
import importlib
from gptsh.mcp.builtin import get_builtin_servers

def _select_servers_file(config: Dict[str, Any]) -> Optional[str]:
    """
    Choose a single MCP servers JSON file based on precedence:
      1) CLI-provided mcp.servers_files (first existing)
      2) Local project ./.gptsh/mcp_servers.json
      3) Global ~/.config/gptsh/mcp_servers.json
    Returns the chosen absolute path, or None if none found.
    """
    mcp_conf = config.get("mcp", {}) or {}
    candidates: List[str] = []

    user_paths = mcp_conf.get("servers_files")
    if isinstance(user_paths, str):
        user_paths = [user_paths]
    if isinstance(user_paths, list):
        for p in user_paths:
            if p:
                candidates.append(os.path.expanduser(str(p)))

    # Project-local then global defaults
    candidates.append(os.path.abspath("./.gptsh/mcp_servers.json"))
    candidates.append(os.path.expanduser("~/.config/gptsh/mcp_servers.json"))

    for path in candidates:
        try:
            if os.path.isfile(path):
                return path
        except Exception:
            continue
    return None

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Discover tools from configured MCP servers using Model Context Protocol Python SDK.
    Runs discovery concurrently and isolates failures per server.
    """
    return asyncio.run(_list_tools_async(config))

async def _list_tools_async(config: Dict[str, Any]) -> Dict[str, List[str]]:
    # Select a single MCP servers file based on precedence (CLI -> local -> global)
    selected_file = _select_servers_file(config)
    servers: Dict[str, Any] = {}
    if selected_file:
        try:
            with open(selected_file, "r", encoding="utf-8") as f:
                raw = f.read()
            # Normalize ${env:VAR} -> ${VAR} first, then expand using existing _expand_env
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            servers = {}
        except Exception:
            servers = {}
    # Ensure builtin stdio-in-process servers are always present by default
    for _name, _def in (get_builtin_servers() or {}).items():
        servers.setdefault(_name, _def)

    # Determine a per-request timeout (fallback to a sensible default)
    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))

    async def _query_server(name: str, srv: Dict[str, Any]) -> List[str]:
        transport = srv.get("transport", {})
        ttype = transport.get("type")
        if not ttype:
            if transport.get("url") or srv.get("url"):
                ttype = "http"
            elif srv.get("command") or srv.get("module"):
                ttype = "stdio"
            else:
                ttype = None
        try:
            if ttype == "stdio":
                module_path = srv.get("module")
                if module_path:
                    try:
                        mod = importlib.import_module(module_path)
                        if not hasattr(mod, "list_tools"):
                            logging.getLogger(__name__).warning("Builtin stdio module '%s' missing list_tools()", module_path)
                            return []
                        tools_list = getattr(mod, "list_tools")() or []
                        return list(tools_list)
                    except Exception as e:
                        logging.getLogger(__name__).warning("Failed loading builtin stdio module '%s': %s", module_path, e, exc_info=True)
                        return []
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

    # Run all server queries concurrently, honoring 'disabled' servers and allowed filter
    allowed = set((config.get("mcp", {}) or {}).get("allowed_servers") or [])
    results_map: Dict[str, List[str]] = {}
    tasks: List[asyncio.Task] = []
    task_names: List[str] = []
    for name, srv in servers.items():
        if srv.get("disabled"):
            # Mark disabled servers with empty tool list and skip querying
            results_map[name] = []
            continue
        if allowed and name not in allowed:
            # Skip servers not explicitly allowed
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

@asynccontextmanager
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
        async with stdio_client(
            params,
            errlog=sys.stderr if logging.getLogger(__name__).getEffectiveLevel() <= logging.DEBUG else asyncio.subprocess.DEVNULL,
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

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

        # Heuristic selection: explicit SSE path or URL hint -> SSE; otherwise try streamable then fallback to SSE.
        use_sse = (ttype == "sse") or bool(re.search(r"/sse(?:$|[/?])", url))
        if use_sse:
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            try:
                async with streamablehttp_client(url, headers=headers) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            except Exception:
                async with sse_client(url, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
    else:
        raise RuntimeError(f"MCP server '{name}' has unknown transport type: {ttype!r}")

def discover_tools_detailed(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Return detailed MCP tool definitions per server:
      { server_name: [ {name, description, input_schema}, ... ] }
    """
    return asyncio.run(_discover_tools_detailed_async(config))

async def _discover_tools_detailed_async(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    # Load from a single servers file selected by precedence (CLI -> local -> global)
    selected_file = _select_servers_file(config)
    servers: Dict[str, Any] = {}
    if selected_file:
        try:
            with open(selected_file, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            servers = {}
        except Exception:
            servers = {}
    # Ensure builtin stdio-in-process servers are always present by default
    for _name, _def in (get_builtin_servers() or {}).items():
        servers.setdefault(_name, _def)

    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
    results: Dict[str, List[Dict[str, Any]]] = {}

    async def _per_server(name: str, srv: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        if srv.get("disabled"):
            return name, []
        try:
            transport = srv.get("transport", {})
            ttype = transport.get("type")
            if ttype == "stdio" and srv.get("module"):
                module_path = srv.get("module")
                try:
                    mod = importlib.import_module(module_path)
                    if not hasattr(mod, "list_tools_detailed"):
                        logging.getLogger(__name__).warning("Builtin stdio module '%s' missing list_tools_detailed()", module_path)
                        return name, []
                    detailed = getattr(mod, "list_tools_detailed")() or []
                    return name, list(detailed)
                except Exception as e:
                    logging.getLogger(__name__).warning("Failed loading builtin stdio module '%s': %s", module_path, e, exc_info=True)
                    return name, []
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

    # Honor allow-list of servers if provided
    allowed = set((config.get("mcp", {}) or {}).get("allowed_servers") or [])
    tasks = [asyncio.create_task(_per_server(n, s)) for n, s in servers.items() if (not allowed or n in allowed)]
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
    # Load servers from a single selected file
    selected_file = _select_servers_file(config)
    servers: Dict[str, Any] = {}
    if selected_file:
        try:
            with open(selected_file, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            servers = {}
        except Exception:
            servers = {}
    # Ensure builtin stdio-in-process servers are always present by default
    for _name, _def in (get_builtin_servers() or {}).items():
        servers.setdefault(_name, _def)
    allowed = set((config.get("mcp", {}) or {}).get("allowed_servers") or [])
    if server not in servers:
        raise RuntimeError(f"MCP server '{server}' not configured")
    if allowed and server not in allowed:
        raise RuntimeError(f"MCP server '{server}' is not allowed by --tools filter")

    timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
    srv = servers[server]
    try:
        # Execute via builtin stdio-in-process module if defined
        if (srv.get("transport", {}) or {}).get("type") == "stdio" and srv.get("module"):
            module_path = srv.get("module")
            try:
                mod = importlib.import_module(module_path)
                if not hasattr(mod, "execute"):
                    raise RuntimeError(f"Builtin stdio module '{module_path}' missing execute()")
                return str(getattr(mod, "execute")(tool, arguments or {}))
            except Exception as e:
                logging.getLogger(__name__).warning("Builtin stdio execution failed for %s:%s via %s: %s", server, tool, module_path, e, exc_info=True)
                raise
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

def get_auto_approved_tools(config: Dict[str, Any], agent_conf: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    Load per-server autoApprove tool lists from configured MCP servers files and merge
    optional agent-level autoApprove directives.

    Returns mapping: server_name -> list of tool names to auto-approve.
    Special cases:
      - If a server's list contains "*", it means all tools for that server are approved.
      - The special server key "*" contains tool names approved across all servers by name.
    Disabled servers are still included if present in config so the UI can display badges,
    but they will typically have no discovered tools.
    """
    selected_file = _select_servers_file(config)
    servers: Dict[str, Any] = {}
    if selected_file:
        try:
            with open(selected_file, "r", encoding="utf-8") as f:
                raw = f.read()
            content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
            content = _expand_env(content)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            pass
        except Exception:
            # If parse fails for the file, ignore it
            pass

    # Merge builtin in-process servers so agent-level entries like 'time' can match a server group
    for _name, _def in (get_builtin_servers() or {}).items():
        servers.setdefault(_name, _def)

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

    # Merge agent-level auto approvals if provided
    if agent_conf and isinstance(agent_conf, dict):
        entries = agent_conf.get("autoApprove")
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                if not entry:
                    continue
                token = str(entry)
                if "__" in token:
                    # Format: "<server>__<tool>"
                    srv_name, tool_name = token.split("__", 1)
                    if srv_name:
                        approved_map.setdefault(srv_name, [])
                        if tool_name and tool_name not in approved_map[srv_name]:
                            approved_map[srv_name].append(tool_name)
                else:
                    # Either a server name or a tool name across all servers
                    if token in servers:
                        # Approve all tools for this server
                        approved_map.setdefault(token, [])
                        if "*" not in approved_map[token]:
                            approved_map[token].append("*")
                    else:
                        # Approve by tool name across all servers
                        approved_map.setdefault("*", [])
                        if token not in approved_map["*"]:
                            approved_map["*"].append(token)

    return approved_map
