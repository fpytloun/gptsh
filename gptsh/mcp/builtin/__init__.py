# In-process stdio-like builtin MCP servers.
# Each module under gptsh.mcp.builtin.<name> must expose:
# - list_tools() -> list[str]
# - list_tools_detailed() -> list[dict]
# - execute(tool: str, arguments: dict) -> str

import importlib
import pkgutil
from typing import Any, Dict


def get_builtin_servers() -> Dict[str, Any]:
    """
    Discover builtin MCP servers implemented as modules under gptsh.mcp.builtin.
    Each valid module must define list_tools, list_tools_detailed, and execute.
    Returns mapping suitable for merging into MCP servers config:
      { "<name>": { "transport": {"type": "stdio"}, "module": "gptsh.mcp.builtin.<name>" }, ... }
    """
    result: Dict[str, Any] = {}
    try:
        import gptsh.mcp.builtin as _pkg
        for m in pkgutil.iter_modules(_pkg.__path__):
            if m.ispkg:
                # Only flat modules are considered servers
                continue
            name = m.name
            module_path = f"{_pkg.__name__}.{name}"
            try:
                mod = importlib.import_module(module_path)
                if all(hasattr(mod, attr) for attr in ("list_tools", "list_tools_detailed", "execute")):
                    result[name] = {
                        "transport": {"type": "stdio"},
                        "module": module_path,
                    }
            except Exception:
                # Skip modules that fail to import or validate
                continue
    except Exception:
        # If discovery fails entirely, return empty set
        return {}
    return result
