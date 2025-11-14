"""Clipboard builtin MCP tool with native and OSC52 support.

Supports reading and writing to system clipboard on macOS and Linux.
- macOS: Uses pasteboard library (Cocoa bindings) if available
- Linux: Uses tkinter (built-in) for clipboard access
- OSC52: Terminal escape sequence for clipboard over SSH

Tools:
  - clipboard_read: Read content from system clipboard
  - clipboard_write: Write content to system clipboard
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default auto-approval for all tools in this builtin server
AUTO_APPROVE_DEFAULT = ["clipboard_read", "clipboard_write"]

# Module-level cache for config (set by caller if needed)
_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def set_config(config: Dict[str, Any]) -> None:
    """Set the config for clipboard tool. Called by MCP client if available."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = config


def _get_clipboard_config() -> Dict[str, Any]:
    """Get clipboard configuration from global cache or environment."""
    if _CONFIG_CACHE:
        return _CONFIG_CACHE.get("clipboard", {})
    return {}


def _is_clipboard_enabled() -> bool:
    """Check if clipboard tool is enabled in config."""
    config = _get_clipboard_config()
    return config.get("enabled", True)


def _get_clipboard_mode() -> str:
    """Get clipboard mode from config: auto, native, both, osc52."""
    config = _get_clipboard_config()
    return config.get("mode", "auto")


def _is_ssh_session() -> bool:
    """Detect if we're running in an SSH session."""
    return bool(
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
        or os.environ.get("SSH_TTY")
    )


def _is_tty() -> bool:
    """Check if stdout is connected to a terminal."""
    return sys.stdout.isatty()


def _detect_platform() -> str:
    """Detect platform: 'macos', 'linux', or 'unsupported'."""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _write_osc52(text: str, clipboard_name: str = "c") -> None:
    """
    Write to clipboard via OSC52 escape sequence.

    Format: ESC ] 52 ; <clipboard_name> ; <base64_data> BEL

    Args:
        text: Text to write to clipboard
        clipboard_name: Clipboard target (c=clipboard, p=primary, s=select)
    """
    if not _is_tty():
        return

    try:
        # Encode text to base64
        data_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")

        # Build OSC52 sequence: ESC ] 52 ; <clipboard> ; <data> BEL
        osc52_seq = f"\033]52;{clipboard_name};{data_b64}\007"

        # Send to stdout (goes to terminal)
        sys.stdout.write(osc52_seq)
        sys.stdout.flush()
    except Exception as e:
        logger.debug("OSC52 write failed: %s", e)


def _read_clipboard_macos() -> str:
    """Read clipboard using pasteboard library (Cocoa bindings on macOS)."""
    try:
        pasteboard = importlib.import_module("pasteboard")
        pb = pasteboard.Pasteboard()
        content = pb.get_contents()
        if content is None:
            return ""
        return str(content)
    except ImportError as e:
        raise RuntimeError(
            "pasteboard library not installed. Install with: pip install pasteboard"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to read clipboard on macOS: {e}") from e


def _write_clipboard_macos(text: str) -> None:
    """Write clipboard using pasteboard library (Cocoa bindings on macOS)."""
    try:
        pasteboard = importlib.import_module("pasteboard")
        pb = pasteboard.Pasteboard()
        pb.set_contents(text)
    except ImportError as e:
        raise RuntimeError(
            "pasteboard library not installed. Install with: pip install pasteboard"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to write clipboard on macOS: {e}") from e


def _read_clipboard_linux() -> str:
    """Read clipboard using tkinter (built-in on Linux)."""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            content = root.clipboard_get()
            return content
        finally:
            root.destroy()
    except Exception as e:
        raise RuntimeError(f"Failed to read clipboard on Linux: {e}") from e


def _write_clipboard_linux(text: str) -> None:
    """Write clipboard using tkinter (built-in on Linux)."""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()  # Required to process clipboard
        finally:
            root.destroy()
    except Exception as e:
        raise RuntimeError(f"Failed to write clipboard on Linux: {e}") from e


def _read_system_clipboard() -> str:
    """Read from system clipboard using platform-specific native method."""
    platform = _detect_platform()
    if platform == "macos":
        return _read_clipboard_macos()
    elif platform == "linux":
        return _read_clipboard_linux()
    else:
        raise RuntimeError(
            f"Clipboard not supported on {sys.platform}. Supported platforms: macOS, Linux"
        )


def _write_system_clipboard(text: str) -> None:
    """Write to system clipboard using platform-specific native method."""
    platform = _detect_platform()
    if platform == "macos":
        return _write_clipboard_macos(text)
    elif platform == "linux":
        return _write_clipboard_linux(text)
    else:
        raise RuntimeError(
            f"Clipboard not supported on {sys.platform}. Supported platforms: macOS, Linux"
        )


def _should_try_osc52(mode: str) -> bool:
    """Determine if OSC52 should be attempted based on mode."""
    if mode == "native":
        return False
    if mode == "osc52":
        return True
    if mode == "both":
        return True
    # mode == "auto": try OSC52 in SSH or when TTY
    if mode == "auto":
        return _is_tty()
    return False


def _tool_clipboard_read(arguments: Dict[str, Any]) -> str:
    """
    MCP Tool: clipboard_read - read content from system clipboard.

    NEVER uses OSC52 (security reasons - reading clipboard via escape sequences
    not supported by most terminals for security).

    Returns JSON: {"ok": true, "content": "..."} or {"ok": false, "error": "..."}
    """
    if not _is_clipboard_enabled():
        return json.dumps({"ok": False, "error": "Clipboard tool disabled in config"})

    try:
        content = _read_system_clipboard()
        return json.dumps({"ok": True, "content": content})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _tool_clipboard_write(arguments: Dict[str, Any]) -> str:
    """
    MCP Tool: clipboard_write - write content to system clipboard.

    Supports multiple methods based on configuration:
    - auto (default): Try OSC52 in SSH, then native
    - native: Only use native clipboard
    - both: Always try both OSC52 and native
    - osc52: Only use OSC52

    Returns JSON:
      - Success: {"ok": true, "method": "native|osc52|both"}
      - Error: {"ok": false, "error": "..."}
    """
    if not _is_clipboard_enabled():
        return json.dumps({"ok": False, "error": "Clipboard tool disabled in config"})

    text = arguments.get("text")
    if not isinstance(text, str):
        return json.dumps({"ok": False, "error": "'text' argument must be a string"})

    mode = _get_clipboard_mode()
    method = None

    try:
        # Determine strategy based on mode
        if mode == "osc52":
            # Only use OSC52
            _write_osc52(text)
            method = "osc52"
        elif mode == "native":
            # Only use native clipboard
            _write_system_clipboard(text)
            method = "native"
        elif mode == "both":
            # Always try both
            try:
                _write_osc52(text)
            except Exception as e:
                logger.debug("OSC52 write in 'both' mode failed: %s", e)
            _write_system_clipboard(text)
            method = "both"
        else:
            # mode == "auto" (default): Smart detection
            # In SSH: try both; Otherwise: native only
            if _is_ssh_session():
                # In SSH session - try both methods
                try:
                    _write_osc52(text)
                except Exception as e:
                    logger.debug("OSC52 write in SSH session failed: %s", e)
                # Always try native as well in case we're in a nested session
                try:
                    _write_system_clipboard(text)
                    method = "both"
                except Exception:
                    method = "osc52"
            else:
                # Local session - use native only
                _write_system_clipboard(text)
                method = "native"

        return json.dumps({"ok": True, "method": method or "native"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def list_tools() -> List[str]:
    """Return list of available tools."""
    return ["clipboard_read", "clipboard_write"]


def list_tools_detailed() -> List[Dict[str, Any]]:
    """Return detailed tool schemas."""
    return [
        {
            "name": "clipboard_read",
            "description": (
                "Read content from the system clipboard. "
                "Supports macOS (pasteboard library) and Linux (tkinter). "
                "Never uses OSC52 for security reasons."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "clipboard_write",
            "description": (
                "Write content to the system clipboard. "
                "Supports macOS (pasteboard library) and Linux (tkinter). "
                "Can use OSC52 escape sequences for SSH sessions (configurable). "
                "Configuration options: clipboard.mode (auto, native, both, osc52), "
                "clipboard.enabled (true/false)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text content to write to clipboard",
                    }
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    ]


def execute(tool: str, arguments: Dict[str, Any]) -> str:
    """
    Execute a clipboard tool.

    Args:
        tool: Tool name ("clipboard_read" or "clipboard_write")
        arguments: Tool arguments as dictionary

    Returns:
        JSON string with result or error
    """
    if tool == "clipboard_read":
        return _tool_clipboard_read(arguments)
    elif tool == "clipboard_write":
        return _tool_clipboard_write(arguments)
    else:
        return json.dumps({"ok": False, "error": f"Unknown tool: clipboard:{tool}"})
