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
    # Placeholder: empty list for each server until transport is implemented
    return {name: [] for name in servers}
