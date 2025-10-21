from __future__ import annotations

import logging
from typing import Any, Dict
from rich.control import Control, ControlType

from gptsh.interfaces import ApprovalPolicy


def _canon(n: str) -> str:
    return str(n).lower().replace("-", "_").strip()


def _best_effort_flush_stdin() -> None:
    """Discard any pending keystrokes (e.g., stray newlines) before prompting.

    Uses termios.tcflush on TTYs (Unix). No-op on unsupported platforms or errors.
    """
    import sys
    try:
        if sys.stdin.isatty():
            import termios  # Unix-only
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception as e:
        # Best-effort: log at debug and continue if unavailable or any error occurs.
        logging.debug("stdin flush skipped (termios/tty unavailable): %s", e, exc_info=True)


class DefaultApprovalPolicy(ApprovalPolicy):
    def __init__(self, approved_map: Dict[str, list[str]] | None = None):
        self._approved = {k: list(v) for k, v in (approved_map or {}).items()}

    def is_auto_allowed(self, server: str, tool: str) -> bool:
        s = self._approved.get(server, [])
        g = self._approved.get("*", [])
        canon_tool = _canon(tool)
        canon_full = _canon(f"{server}__{tool}")
        s_c = {_canon(x) for x in s}
        g_c = {_canon(x) for x in g}
        return (
            "*" in s
            or "*" in g
            or canon_tool in s_c
            or canon_tool in g_c
            or canon_full in s_c
            or canon_full in g_c
        )

    async def confirm(self, server: str, tool: str, args: Dict[str, Any]) -> bool:
        # Deny in non-interactive environments for safety
        import sys
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            return False
        try:
            from rich.console import Console
            from rich.prompt import Confirm
        except Exception:
            Confirm = None  # type: ignore
            Console = None  # type: ignore
        if Confirm is None or Console is None:
            return False
        import json

        arg_text = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        console = Console(stderr=True, soft_wrap=True)
        # Clear any pending input (e.g., stray Enters) before asking
        _best_effort_flush_stdin()
        choice = Confirm.ask(
            f"[grey50]Allow tool[/grey50] [dim yellow]{server}__{tool}[/dim yellow] [grey50]with args[/grey50] [dim]{arg_text}[/dim]?",
            choices=["y", "n"],
            case_sensitive=False,
            default=False,
            console=console,
        )

        # Cleanup confirm prompt
        console.control(
            Control.move(y=-1),
            Control.move_to_column(0),
            Control((ControlType.ERASE_IN_LINE, 2)),
        )

        return bool(choice)
