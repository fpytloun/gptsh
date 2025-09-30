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

# Per-event-loop MCP session manager to spawn/connect servers once and reuse them
_MANAGERS: Dict[int, "_MCPManager"] = {}

class _MCPManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
        spawn_conf = (config.get("mcp", {}) or {}).get("spawn", {}) or {}
        hc_conf = (spawn_conf.get("healthcheck", {}) or {})
        self._hc_type: str = str(hc_conf.get("type") or "initialize")
        try:
            self._hc_timeout: float = float(hc_conf.get("timeout")) if "timeout" in hc_conf else self.timeout_seconds
        except Exception:
            self._hc_timeout = self.timeout_seconds
        self.servers: Dict[str, Any] = {}
        # name -> ("module", module_path) or ("session", ClientSession) or None if disabled/unavailable/filtered
        self.sessions: Dict[str, Optional[Tuple[str, Any]]] = {}
        self._server_tasks: Dict[str, asyncio.Task] = {}
        self._ready_events: Dict[str, asyncio.Event] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}
        self.started: bool = False

    async def start(self) -> None:
        if self.started:
            return
        logger = logging.getLogger(__name__)
        logger.debug("Starting MCP manager (timeout=%.1fs)", self.timeout_seconds)
        # Build servers dict using single selected servers file and merge builtins
        selected_file = _select_servers_file(self.config)
        servers: Dict[str, Any] = {}
        if selected_file:
            try:
                with open(selected_file, "r", encoding="utf-8") as f:
                    raw = f.read()
                content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
                content = _expand_env(content)
                data = json.loads(content)
                servers.update(data.get("mcpServers", {}))
                logger.debug("Selected MCP servers file: %s", selected_file)
            except Exception as e:
                logger.warning("Failed to parse MCP servers file %s: %s", selected_file, e, exc_info=True)
                servers = {}
        for _name, _def in (get_builtin_servers() or {}).items():
            servers.setdefault(_name, _def)

        self.servers = servers
        allowed = set((self.config.get("mcp", {}) or {}).get("allowed_servers") or [])
        if allowed:
            logger.debug("Allowed MCP servers filter: %s", ", ".join(sorted(allowed)))
        logger.debug("Discovered MCP servers: %s", ", ".join(sorted(self.servers.keys())) or "(none)")

        async def _runner(name: str, srv: Dict[str, Any], stop_event: asyncio.Event, ready_event: asyncio.Event) -> None:
            try:
                if srv.get("disabled") or (allowed and name not in allowed):
                    logging.getLogger(__name__).debug("Server '%s' is disabled or filtered; skipping", name)
                    self.sessions[name] = None
                    ready_event.set()
                    await stop_event.wait()
                    return
                transport = srv.get("transport", {})
                ttype = transport.get("type")
                if not ttype:
                    if transport.get("url") or srv.get("url"):
                        ttype = "http"
                    elif srv.get("command") or srv.get("module"):
                        ttype = "stdio"
                    else:
                        ttype = None
                logging.getLogger(__name__).debug("Server '%s' transport resolved to %r", name, ttype)
                # Builtin in-process module server
                if ttype == "stdio" and srv.get("module"):
                    module_path = srv.get("module")
                    try:
                        importlib.import_module(module_path)
                        self.sessions[name] = ("module", module_path)
                    except Exception as e:
                        logging.getLogger(__name__).warning("Failed loading builtin stdio module '%s': %s", module_path, e, exc_info=True)
                        self.sessions[name] = None
                    finally:
                        logging.getLogger(__name__).debug("Server '%s' ready (module=%s)", name, "ok" if self.sessions.get(name) else "failed")
                        ready_event.set()
                    await stop_event.wait()
                    return
                if ttype == "stdio":
                    params = StdioServerParameters(
                        command=srv.get("command"),
                        args=srv.get("args", []),
                        env=srv.get("env", {}),
                    )
                    logging.getLogger(__name__).debug("Connecting MCP stdio server '%s' (command=%r)", name, params.command)
                    async with stdio_client(
                        params,
                        errlog=sys.stderr if logging.getLogger(__name__).getEffectiveLevel() <= logging.DEBUG else asyncio.subprocess.DEVNULL,
                    ) as (read, write):
                        async with ClientSession(read, write) as session:
                            try:
                                await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                if self._hc_type == "list_tools":
                                    try:
                                        await asyncio.wait_for(session.list_tools(), timeout=self._hc_timeout)
                                    except Exception as e:
                                        logging.getLogger(__name__).warning("Healthcheck list_tools failed for '%s': %s", name, e, exc_info=True)
                            except Exception as e:
                                logging.getLogger(__name__).warning("Initialization failed for MCP stdio server '%s' after %.1fs: %s", name, self._hc_timeout, e, exc_info=True)
                                self.sessions[name] = None
                                ready_event.set()
                                return
                            self.sessions[name] = ("session", session)
                            logging.getLogger(__name__).debug("Server '%s' ready (stdio)", name)
                            ready_event.set()
                            await stop_event.wait()
                            return
                elif ttype in ("http", "sse"):
                    url = transport.get("url") or srv.get("url")
                    if not url:
                        self.sessions[name] = None
                        ready_event.set()
                        await stop_event.wait()
                        return
                    headers = (
                        srv.get("credentials", {}).get("headers")
                        or transport.get("headers")
                        or srv.get("headers")
                        or {}
                    )
                    use_sse = (ttype == "sse") or bool(re.search(r"/sse(?:$|[/?])", url))
                    if use_sse:
                        logging.getLogger(__name__).debug("Connecting MCP SSE server '%s'", name)
                        async with sse_client(url, headers=headers) as (read, write):
                            async with ClientSession(read, write) as session:
                                try:
                                    await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                    if self._hc_type == "list_tools":
                                        try:
                                            await asyncio.wait_for(session.list_tools(), timeout=self._hc_timeout)
                                        except Exception as e:
                                            logging.getLogger(__name__).warning("Healthcheck list_tools failed for '%s': %s", name, e, exc_info=True)
                                except Exception as e:
                                    logging.getLogger(__name__).warning("Initialization failed for MCP SSE server '%s' after %.1fs: %s", name, self._hc_timeout, e, exc_info=True)
                                    self.sessions[name] = None
                                    ready_event.set()
                                    return
                                    self.sessions[name] = ("session", session)
                                    logging.getLogger(__name__).debug("Server '%s' ready (sse)", name)
                                    ready_event.set()
                                    await stop_event.wait()
                                    return
                    else:
                        try:
                            logging.getLogger(__name__).debug("Connecting MCP HTTP server '%s'", name)
                            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                                async with ClientSession(read, write) as session:
                                    try:
                                        await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                        if self._hc_type == "list_tools":
                                            try:
                                                await asyncio.wait_for(session.list_tools(), timeout=self._hc_timeout)
                                            except Exception as e:
                                                logging.getLogger(__name__).warning("Healthcheck list_tools failed for '%s': %s", name, e, exc_info=True)
                                    except Exception as e:
                                        logging.getLogger(__name__).warning("Initialization failed for MCP HTTP server '%s' after %.1fs: %s", name, self._hc_timeout, e, exc_info=True)
                                        self.sessions[name] = None
                                        ready_event.set()
                                        return
                                    self.sessions[name] = ("session", session)
                                    logging.getLogger(__name__).debug("Server '%s' ready (http)", name)
                                    ready_event.set()
                                    await stop_event.wait()
                                    return
                        except Exception:
                            logging.getLogger(__name__).debug("HTTP connect failed; falling back to SSE for '%s'", name)
                            async with sse_client(url, headers=headers) as (read, write):
                                async with ClientSession(read, write) as session:
                                    try:
                                        await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                        if self._hc_type == "list_tools":
                                            try:
                                                await asyncio.wait_for(session.list_tools(), timeout=self._hc_timeout)
                                            except Exception as e:
                                                logging.getLogger(__name__).warning("Healthcheck list_tools failed for '%s': %s", name, e, exc_info=True)
                                    except Exception as e:
                                        logging.getLogger(__name__).warning("Initialization failed for MCP SSE server '%s' after %.1fs: %s", name, self._hc_timeout, e, exc_info=True)
                                        self.sessions[name] = None
                                        ready_event.set()
                                        return
                                    self.sessions[name] = ("session", session)
                                    logging.getLogger(__name__).debug("Server '%s' ready (sse)", name)
                                    ready_event.set()
                                    await stop_event.wait()
                                    return
                else:
                    self.sessions[name] = None
                    ready_event.set()
                    await stop_event.wait()
            except Exception as e:
                logging.getLogger(__name__).warning("Failed to connect to MCP server '%s': %s", name, e, exc_info=True)
                self.sessions[name] = None
                ready_event.set()
                try:
                    await stop_event.wait()
                except Exception:
                    pass

        # Spawn runners in parallel and wait until each signals ready or times out
        for name, srv in servers.items():
            stop_event = asyncio.Event()
            ready_event = asyncio.Event()
            self._stop_events[name] = stop_event
            self._ready_events[name] = ready_event
            task = asyncio.create_task(_runner(name, srv, stop_event, ready_event))
            self._server_tasks[name] = task

        # Wait for readiness for all servers; block until each signals ready or times out
        waiters: List[asyncio.Task] = []
        order: List[str] = []
        for name, ev in self._ready_events.items():
            order.append(name)
            waiters.append(asyncio.create_task(asyncio.wait_for(ev.wait(), timeout=self.timeout_seconds)))
        if waiters:
            results = await asyncio.gather(*waiters, return_exceptions=True)
            for name, res in zip(order, results):
                if isinstance(res, Exception):
                    logger.warning("MCP server '%s' readiness timed out after %.1fs", name, self._hc_timeout)
                else:
                    logger.debug("MCP server '%s' signaled ready", name)

        self.started = True
        logger.debug("MCP manager start complete")

    async def stop(self) -> None:
        # Signal all server runners to stop and wait for them
        for ev in self._stop_events.values():
            try:
                ev.set()
            except Exception:
                pass
        tasks = list(self._server_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._server_tasks.clear()
        self._ready_events.clear()
        self._stop_events.clear()
        self.sessions.clear()
        self.started = False

    async def list_tools(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        for name, srv in self.servers.items():
            if srv.get("disabled"):
                result[name] = []
                continue
            handle = self.sessions.get(name)
            try:
                if handle and handle[0] == "module":
                    mod = importlib.import_module(handle[1])
                    tools_list = getattr(mod, "list_tools")() or []
                    result[name] = list(tools_list)
                elif handle and handle[0] == "session":
                    session = handle[1]
                    resp = await session.list_tools()
                    result[name] = [tool.name for tool in resp.tools]
                else:
                    result[name] = []
            except Exception as e:
                logging.getLogger(__name__).warning("MCP list_tools failed for '%s': %s", name, e, exc_info=True)
                result[name] = []
        return result

    async def list_tools_detailed(self) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for name, srv in self.servers.items():
            if srv.get("disabled"):
                result[name] = []
                continue
            handle = self.sessions.get(name)
            try:
                if handle and handle[0] == "module":
                    mod = importlib.import_module(handle[1])
                    detailed = getattr(mod, "list_tools_detailed")() or []
                    result[name] = list(detailed)
                elif handle and handle[0] == "session":
                    session = handle[1]
                    resp = await session.list_tools()
                    out: List[Dict[str, Any]] = []
                    for tool in resp.tools:
                        schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}, "additionalProperties": True}
                        desc = getattr(tool, "description", None) or ""
                        out.append({
                            "name": tool.name,
                            "description": desc,
                            "input_schema": schema,
                        })
                    result[name] = out
                else:
                    result[name] = []
            except Exception as e:
                logging.getLogger(__name__).warning("MCP list_tools_detailed failed for '%s': %s", name, e, exc_info=True)
                result[name] = []
        return result

    async def call_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> str:
        srv = self.servers.get(server) or {}
        if srv.get("disabled"):
            raise RuntimeError(f"MCP server '{server}' is disabled")
        handle = self.sessions.get(server)
        if handle and handle[0] == "module":
            mod = importlib.import_module(handle[1])
            return str(getattr(mod, "execute")(tool, arguments or {}))
        if handle and handle[0] == "session":
            session = handle[1]
            resp = await session.call_tool(tool, arguments or {})
            texts: List[str] = []
            for item in getattr(resp, "content", []) or []:
                t = getattr(item, "text", None)
                if t is not None:
                    texts.append(str(t))
                else:
                    try:
                        texts.append(str(item))
                    except Exception:
                        pass
            return "\n".join(texts).strip()
        raise RuntimeError(f"MCP server '{server}' not configured or not connected")

async def ensure_sessions_started_async(config: Dict[str, Any]) -> _MCPManager:
    loop_id = id(asyncio.get_running_loop())
    mgr = _MANAGERS.get(loop_id)
    if mgr is None:
        mgr = _MCPManager(config)
        _MANAGERS[loop_id] = mgr
    if not mgr.started:
        await mgr.start()
    return mgr

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Discover tools from configured MCP servers using Model Context Protocol Python SDK.
    Runs discovery concurrently and isolates failures per server.
    """
    return asyncio.run(_list_tools_async(config))

async def _list_tools_async(config: Dict[str, Any]) -> Dict[str, List[str]]:
    # Ensure servers are spawned once and reused; query via persistent sessions
    mgr = await ensure_sessions_started_async(config)
    return await mgr.list_tools()

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
    # Use persistent sessions; servers are connected in parallel on first call
    mgr = await ensure_sessions_started_async(config)
    return await mgr.list_tools_detailed()

def execute_tool(server: str, tool: str, arguments: Dict[str, Any], config: Dict[str, Any]) -> str:
    """
    Execute a single MCP tool call and return concatenated string content result.
    """
    return asyncio.run(_execute_tool_async(server, tool, arguments, config))

async def _execute_tool_async(server: str, tool: str, arguments: Dict[str, Any], config: Dict[str, Any]) -> str:
    # Execute using persistent session for the given server
    mgr = await ensure_sessions_started_async(config)
    return await mgr.call_tool(server, tool, arguments)

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
