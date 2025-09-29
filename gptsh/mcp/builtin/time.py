from __future__ import annotations
from typing import Any, Dict, List
from datetime import datetime, timezone

def list_tools() -> List[str]:
    return ["now"]

def list_tools_detailed() -> List[Dict[str, Any]]:
    return [{
        "name": "now",
        "description": "Return the current UTC time in ISO 8601 format (UTC).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }]

def execute(tool: str, arguments: Dict[str, Any]) -> str:
    if tool == "now":
        # ISO 8601 with Z suffix for UTC
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    raise RuntimeError(f"Unknown tool: time:{tool}")
