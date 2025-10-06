from __future__ import annotations

from typing import Any, Dict, List, Optional

from gptsh.mcp.client import (
    discover_tools_detailed as _discover_tools_detailed,
    get_auto_approved_tools as _get_auto_approved_tools,
    list_tools as _list_tools,
)


def list_tools(config: Dict[str, Any]) -> Dict[str, List[str]]:
    return _list_tools(config)


def discover_tools_detailed(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    return _discover_tools_detailed(config)


def get_auto_approved_tools(config: Dict[str, Any], agent_conf: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    return _get_auto_approved_tools(config, agent_conf=agent_conf)
