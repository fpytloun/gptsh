from __future__ import annotations

from typing import Any, Dict

from gptsh.interfaces import ApprovalPolicy


def _canon(n: str) -> str:
    return str(n).lower().replace("-", "_").strip()


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
        if not sys.stdin.isatty() or not sys.stdout.isatty():
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
        # Use a subtle dim yellow style for approval prompts
        console = Console()
        # Rich's Confirm.ask doesn’t accept style directly; print a styled preface
        console.print(
            f"[grey50]Allow tool[/grey50] [dim yellow]{server}__{tool}[/dim yellow] [grey50]with args[/grey50] [dim]{arg_text}[/dim]?",
            end=" ",
        )
        return bool(Confirm.ask("[dim yellow][y/N][/dim yellow]", default=False))
