import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx  # noqa: F401  # may be used by underlying MCP clients/transports
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from gptsh.config.loader import _expand_env
from gptsh.core.exceptions import ConfigError
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

    # Prefer explicit CLI-provided paths if set
    user_paths = mcp_conf.get("servers_files_cli") or mcp_conf.get("servers_files")
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

def _parse_servers_value(value: Any) -> Dict[str, Any]:
    """
    Parse a servers mapping from either a dict (YAML) or a JSON string.
    If the JSON payload contains a top-level 'mcpServers', unwrap it.
    Environment variables inside strings are expanded.
    """
    servers: Dict[str, Any] = {}
    if isinstance(value, dict):
        # Support direct YAML mapping or a nested Claude-compatible mapping
        if "mcpServers" in value and isinstance(value["mcpServers"], dict):
            servers = dict(value["mcpServers"])  # unwrap if user pasted JSON-style structure into YAML
        else:
            servers = dict(value)
        return servers
    if isinstance(value, str):
        content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", value)
        content = _expand_env(content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in mcp.servers: {e}") from e
        if isinstance(data, dict) and "mcpServers" in data and isinstance(data["mcpServers"], dict):
            return dict(data["mcpServers"])  # unwrap Claude-compatible schema
        if isinstance(data, dict):
            return dict(data)
        # Parsed successfully but not a mapping; treat as empty mapping
        return {}
    return servers

def _compute_effective_servers(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute the effective servers mapping using precedence:
      1) config.mcp.servers_override (per-agent injection)
      2) config.mcp.servers (global inline YAML or JSON string)
      3) First existing servers file (CLI paths > local > global)
      4) Built-in servers are always present (added if missing)
    """
    mcp_conf = (config.get("mcp") or {})
    servers: Dict[str, Any] = {}

    # 0) If CLI provided file paths explicitly and no inline servers exist, prefer files
    cli_paths = (mcp_conf.get("servers_files_cli") or []) if not mcp_conf.get("servers") else []
    if cli_paths:
        # Force file-based selection path by setting servers to empty and relying on _select_servers_file
        pass
    # Track source for diagnostics only (unused for logic)
    # source: str = "none"
    # 1) Per-agent override
    if "servers_override" in mcp_conf and mcp_conf["servers_override"] and not cli_paths:
        servers = _parse_servers_value(mcp_conf["servers_override"]) or {}
        pass
    # 2) Global inline servers
    elif "servers" in mcp_conf and mcp_conf["servers"] and not cli_paths:
        servers = _parse_servers_value(mcp_conf["servers"]) or {}
        pass
    else:
        # 3) File-based fallback
        selected_file = _select_servers_file(config)
        if selected_file:
            try:
                with open(selected_file, "r", encoding="utf-8") as f:
                    raw = f.read()
                content = re.sub(r"\$\{env:([A-Za-z_]\w*)\}", r"${\1}", raw)
                content = _expand_env(content)
                data = json.loads(content)
                servers.update(data.get("mcpServers", {}))
                logging.getLogger(__name__).debug("Selected MCP servers file: %s", selected_file)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Failed to parse MCP servers file %s: %s", selected_file, e, exc_info=True
                )
                servers = {}

    # 4) Builtins merge: always merge built-ins unless explicitly overridden by same key.
    inject_builtins = mcp_conf.get("servers_override_builtins") or {}
    for _name, _def in (get_builtin_servers() or {}).items():
        servers.setdefault(_name, _def)
    if isinstance(inject_builtins, dict) and inject_builtins:
        for _name, _def in inject_builtins.items():
            servers.setdefault(_name, _def)
    return servers

def _servers_signature(servers: Dict[str, Any]) -> str:
    """Return a stable signature for a servers mapping for caching managers."""
    try:
        payload = json.dumps(servers, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = str(servers)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

# Per-event-loop MCP session manager to spawn/connect servers once and reuse them
_MANAGERS: Dict[Tuple[int, str, str], "_MCPManager"] = {}

class _MCPManager:
    def __init__(self, config: Dict[str, Any], servers: Optional[Dict[str, Any]] = None):
        self.config = config
        self.timeout_seconds: float = float(config.get("timeouts", {}).get("request_seconds", 30))
        spawn_conf = (config.get("mcp", {}) or {}).get("spawn", {}) or {}
        hc_conf = (spawn_conf.get("healthcheck", {}) or {})
        self._hc_type: str = str(hc_conf.get("type") or "initialize")
        try:
            self._hc_timeout: float = float(hc_conf.get("timeout")) if "timeout" in hc_conf else self.timeout_seconds
        except Exception:
            self._hc_timeout = self.timeout_seconds
        self.servers: Dict[str, Any] = dict(servers or {})
        # name -> ("module", module_path) or ("session", ClientSession) or None if disabled/unavailable/filtered
        self.sessions: Dict[str, Optional[Tuple[str, Any]]] = {}
        self._server_tasks: Dict[str, asyncio.Task] = {}
        self._ready_events: Dict[str, asyncio.Event] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}
        self.started: bool = False
        # Capture initialize().instructions per server, if provided by MCP implementation
        self.server_instructions: Dict[str, str] = {}

    async def start(self) -> None:
        if self.started:
            return
        logger = logging.getLogger(__name__)
        logger.debug("Starting MCP manager (timeout=%.1fs)", self.timeout_seconds)
        # Servers were precomputed; ensure a dict copy
        self.servers = dict(self.servers or {})
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
                                init_resp = await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                # Capture optional server-level instructions from initialize response
                                instr = getattr(init_resp, "instructions", None)
                                if isinstance(instr, str) and instr.strip():
                                    self.server_instructions[name] = instr.strip()
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
                                    init_resp = await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                    instr = getattr(init_resp, "instructions", None)
                                    if isinstance(instr, str) and instr.strip():
                                        self.server_instructions[name] = instr.strip()
                                    if self._hc_type == "list_tools":
                                        try:
                                            await asyncio.wait_for(session.list_tools(), timeout=self._hc_timeout)
                                        except Exception as e:
                                            logging.getLogger(__name__).warning("Healthcheck list_tools failed for '%s': %s", name, e, exc_info=True)
                                    self.sessions[name] = ("session", session)
                                    logging.getLogger(__name__).debug("Server '%s' ready (sse)", name)
                                    ready_event.set()
                                    await stop_event.wait()
                                    return
                                except Exception as e:
                                    logging.getLogger(__name__).warning("Initialization failed for MCP SSE server '%s' after %.1fs: %s", name, self._hc_timeout, e, exc_info=True)
                                    self.sessions[name] = None
                                    ready_event.set()
                                    return
                    else:
                        try:
                            logging.getLogger(__name__).debug("Connecting MCP HTTP server '%s'", name)
                            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                                async with ClientSession(read, write) as session:
                                    try:
                                        init_resp = await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                        instr = getattr(init_resp, "instructions", None)
                                        if isinstance(instr, str) and instr.strip():
                                            self.server_instructions[name] = instr.strip()
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
                                        init_resp = await asyncio.wait_for(session.initialize(), timeout=self._hc_timeout)
                                        instr = getattr(init_resp, "instructions", None)
                                        if isinstance(instr, str) and instr.strip():
                                            self.server_instructions[name] = instr.strip()
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
        for name, srv in self.servers.items():
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
            for name, res in zip(order, results, strict=False):
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
                    tools_list = mod.list_tools() or []
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
                    detailed = mod.list_tools_detailed() or []
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
        logger = logging.getLogger(__name__)
        logger.debug("MCP call_tool start: server=%s tool=%s args_keys=%s", server, tool, list((arguments or {}).keys()))
        srv = self.servers.get(server) or {}
        if srv.get("disabled"):
            raise RuntimeError(f"MCP server '{server}' is disabled")
        handle = self.sessions.get(server)
        if handle and handle[0] == "module":
            mod = importlib.import_module(handle[1])
            try:
                out = str(mod.execute(tool, arguments or {}))
                logger.debug("MCP call_tool done (module): server=%s tool=%s", server, tool)
                return out
            except Exception as e:
                logger.warning("MCP module tool error: %s.%s: %s", server, tool, e, exc_info=True)
                raise
        if handle and handle[0] == "session":
            session = handle[1]
            logger.debug("MCP calling session tool: %s.%s", server, tool)
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
            out = "\n".join(texts).strip()
            logger.debug("MCP call_tool done (session): server=%s tool=%s len(out)=%d", server, tool, len(out))
            return out
        raise RuntimeError(f"MCP server '{server}' not configured or not connected")

    async def get_server_instructions(self) -> Dict[str, str]:
        """Return per-server initialize().instructions captured at startup."""
        return dict(self.server_instructions)

async def ensure_sessions_started_async(config: Dict[str, Any]) -> _MCPManager:
    loop_id = id(asyncio.get_running_loop())
    servers = _compute_effective_servers(config)
    sig = _servers_signature(servers)
    mcp_conf = (config.get("mcp") or {})
    allowed_list = list((mcp_conf.get("allowed_servers") or []))
    repl_nonce = str(mcp_conf.get("_repl_nonce") or "0")
    try:
        allowed_sig = ",".join(sorted(str(x) for x in allowed_list))
    except Exception:
        allowed_sig = ""
    key = (loop_id, sig, f"{allowed_sig}|{repl_nonce}")
    mgr = _MANAGERS.get(key)
    if mgr is None:
        mgr = _MCPManager(config, servers=servers)
        _MANAGERS[key] = mgr
    if not mgr.started:
        await mgr.start()
    return mgr

async def stop_all_sessions_async() -> None:
    """
    Stop and clear all MCP managers associated with the current event loop.
    Useful when switching agents or changing effective servers so stale
    sessions are not kept around.
    """
    loop_id = id(asyncio.get_running_loop())
    # Collect keys for this loop
    keys = [k for k in list(_MANAGERS.keys()) if isinstance(k, tuple) and k and k[0] == loop_id]
    managers = [
        _MANAGERS.get(k) for k in keys
    ]
    # Stop managers
    if managers:
        await asyncio.gather(*[m.stop() for m in managers if m], return_exceptions=True)
    # Remove from cache
    for k in keys:
        _MANAGERS.pop(k, None)

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

async def _discover_server_instructions_async(config: Dict[str, Any]) -> Dict[str, str]:
    """Return { server_name: instructions } captured during initialize for active servers."""
    mgr = await ensure_sessions_started_async(config)
    return await mgr.get_server_instructions()

def discover_server_instructions(config: Dict[str, Any]) -> Dict[str, str]:
    """Synchronous helper to fetch server instructions using a new event loop."""
    return asyncio.run(_discover_server_instructions_async(config))

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
    # Use the same precedence as for session startup; but if agent defines custom servers,
    # do not inherit global approvals — compute servers from the agent override only.
    effective_conf = config
    try:
        if isinstance(agent_conf, dict):
            a_mcp = agent_conf.get("mcp") or {}
            if isinstance(a_mcp, dict) and "servers" in a_mcp:
                eff = dict(config or {})
                mcp_cfg = dict((eff.get("mcp") or {}))
                mcp_cfg["servers_override"] = a_mcp.get("servers")
                # Remove other server sources to avoid inheriting global approvals
                mcp_cfg.pop("servers", None)
                mcp_cfg.pop("servers_files_cli", None)
                mcp_cfg.pop("servers_files", None)
                eff["mcp"] = mcp_cfg
                effective_conf = eff
    except Exception:
        pass

    servers: Dict[str, Any] = _compute_effective_servers(effective_conf)

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
