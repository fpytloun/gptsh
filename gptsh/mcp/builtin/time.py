from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


def _resolve_timezone(tz_name: Optional[str]) -> timezone | ZoneInfo:
    """
    Resolve a timezone name to a tzinfo object.
    - If tz_name is None, return the system's current local timezone.
    - If tz_name is 'UTC' (any case), return UTC.
    - Otherwise, try ZoneInfo(tz_name); raise on failure.
    """
    if tz_name is None:
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz or timezone.utc
    if tz_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception as e:
        raise RuntimeError(f"Unknown timezone: {tz_name}") from e

def _isoformat_with_z(dt: datetime) -> str:
    """
    ISO 8601 string; use 'Z' suffix if UTC offset is zero.
    """
    s = dt.isoformat()
    try:
        if dt.utcoffset() == timedelta(0):
            # Normalize any +00:00 to Z
            if s.endswith("+00:00"):
                s = s[:-6] + "Z"
    except Exception:
        pass
    return s

def list_tools() -> List[str]:
    return ["now", "get_current_timezone", "convert_timezone"]

def list_tools_detailed() -> List[Dict[str, Any]]:
    return [
        {
            "name": "now",
            "description": "Return the current time in ISO 8601 format. Defaults to the current local timezone; accepts an optional 'timezone' (IANA name or 'UTC').",
            "input_schema": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name (e.g., 'Europe/Prague') or 'UTC'. Defaults to current local timezone.",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_current_timezone",
            "description": "Return the system's current local timezone name when available; otherwise a descriptive label.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "convert_timezone",
            "description": "Convert an ISO 8601 datetime between timezones.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "datetime": {
                        "type": "string",
                        "description": "Input datetime in ISO 8601. 'Z' is supported for UTC. If naive and 'from_timezone' not provided, current local timezone is assumed.",
                    },
                    "to_timezone": {
                        "type": "string",
                        "description": "Target timezone (IANA name like 'Asia/Tokyo' or 'UTC').",
                    },
                    "from_timezone": {
                        "type": "string",
                        "description": "Source timezone if 'datetime' is naive (no offset). If omitted, current local timezone is used.",
                    },
                },
                "required": ["datetime", "to_timezone"],
                "additionalProperties": False,
            },
        },
    ]

def execute(tool: str, arguments: Dict[str, Any]) -> str:
    if tool == "now":
        tz_name = arguments.get("timezone")
        tzinfo = _resolve_timezone(tz_name)  # current local if None
        now_dt = datetime.now(tzinfo)
        return _isoformat_with_z(now_dt)

    if tool == "get_current_timezone":
        tz = datetime.now().astimezone().tzinfo
        if tz is None:
            return "UTC"
        # Prefer zoneinfo key when available, else tzname
        name = getattr(tz, "key", None) or tz.tzname(None) or str(tz)
        return str(name)

    if tool == "convert_timezone":
        dt_str = arguments.get("datetime")
        to_tz_name = arguments.get("to_timezone")
        from_tz_name = arguments.get("from_timezone")
        if not isinstance(dt_str, str) or not isinstance(to_tz_name, str):
            raise RuntimeError("convert_timezone requires 'datetime' (string) and 'to_timezone' (string)")
        # Normalize 'Z' to '+00:00' for fromisoformat
        norm = dt_str.replace("Z", "+00:00") if isinstance(dt_str, str) else dt_str
        try:
            src_dt = datetime.fromisoformat(norm)
        except Exception as e:
            raise RuntimeError(f"Invalid datetime format: {dt_str}") from e
        # Attach source tz if naive
        if src_dt.tzinfo is None:
            src_tz = _resolve_timezone(from_tz_name) if from_tz_name else _resolve_timezone(None)
            src_dt = src_dt.replace(tzinfo=src_tz)
        # Convert
        target_tz = _resolve_timezone(to_tz_name)
        out_dt = src_dt.astimezone(target_tz)
        return _isoformat_with_z(out_dt)

    raise RuntimeError(f"Unknown tool: time:{tool}")
