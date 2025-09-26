import json
import os
from typing import Any, Dict, List
from gptsh.config.loader import _expand_env

def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Load MCP server definitions from config and return placeholder tool lists.
    TODO: Replace stub with real stdio/SSE/HTTP discovery logic.
    """
    servers_files = config.get("mcp", {}).get("servers_files", [])
    servers = {}
    for path in servers_files:
        expanded = os.path.expanduser(path)
        try:
            raw = open(expanded).read()
            content = _expand_env(raw)
            data = json.loads(content)
            servers.update(data.get("mcpServers", {}))
        except FileNotFoundError:
            continue
    import subprocess
    results: Dict[str, List[str]] = {}
    for name, srv in servers.items():
        transport = srv.get("transport", {})
        if transport.get("type") == "stdio":
            cmd = [srv.get("command")] + srv.get("args", [])
            req = json.dumps({"jsonrpc":"2.0","id":1,"method":"list_tools","params":[]})
            try:
                proc = subprocess.run(cmd, input=req+"\n", capture_output=True, text=True, check=True)
                resp = json.loads(proc.stdout)
                results[name] = resp.get("result", [])
            except Exception:
                results[name] = []
        else:
            results[name] = []
    return results
