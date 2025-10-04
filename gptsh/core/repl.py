from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import click

from gptsh.core.config_api import compute_tools_policy
from gptsh.mcp import ensure_sessions_started_async


class ReplExit(Exception):
    pass


def build_prompt(
    *,
    agent_name: Optional[str],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]],
    cli_model_override: Optional[str],
    readline_enabled: bool,
) -> str:
    chosen = (
        cli_model_override
        or (agent_conf or {}).get("model")
        or provider_conf.get("model")
        or "?"
    )
    model_label = str(chosen).rsplit("/", 1)[-1]
    agent_label = agent_name or "default"
    agent_col = click.style(agent_label, fg="cyan", bold=True)
    model_col = click.style(model_label, fg="magenta")
    return (
        re.sub('(\x1b\\[[0-9;]*[A-Za-z])', r'\001\1\002', f"{agent_col}|{model_col}> ")
        if readline_enabled
        else f"{agent_col}|{model_col}> "
    )


def command_exit() -> None:
    raise ReplExit()


def command_model(
    arg: Optional[str],
    *,
    agent_conf: Optional[Dict[str, Any]],
    provider_conf: Dict[str, Any],
    cli_model_override: Optional[str],
    agent_name: Optional[str],
    readline_enabled: bool,
) -> Tuple[Optional[str], str]:
    if not arg:
        raise ValueError("Usage: /model <model>")
    cli_model_override = arg.strip()
    prompt_str = build_prompt(
        agent_name=agent_name,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        readline_enabled=readline_enabled,
    )
    return cli_model_override, prompt_str


def command_reasoning_effort(
    arg: Optional[str], agent_conf: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    if not arg:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    val = arg.strip().lower()
    if val not in {"minimal", "low", "medium", "high"}:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    if not isinstance(agent_conf, dict):
        agent_conf = {}
    agent_conf["reasoning_effort"] = val
    return agent_conf


def command_agent(
    arg: Optional[str],
    *,
    config: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]],
    agent_name: Optional[str],
    provider_conf: Dict[str, Any],
    cli_model_override: Optional[str],
    no_tools: bool,
    mgr: Any,
    loop: Any,
    readline_enabled: bool,
) -> Tuple[Dict[str, Any], str, str, bool, Any]:
    if not arg:
        raise ValueError("Usage: /agent <agent>")
    new_agent = arg.strip()
    agents_conf_all = config.get("agents") or {}
    if new_agent not in agents_conf_all:
        raise ValueError(f"Unknown agent '{new_agent}'")
    # Switch agent config
    agent_conf = agents_conf_all.get(new_agent) or {}
    agent_name = new_agent
    # Reset model override to the new agent's model (if provided)
    cli_model_override = (agent_conf.get("model") if isinstance(agent_conf, dict) else None)
    # Apply tools policy via config helpers
    labels = None  # REPL command didn't specify CLI labels; rely on agent config
    no_tools, allowed = compute_tools_policy(agent_conf, labels, no_tools)
    if allowed is not None:
        config.setdefault("mcp", {})["allowed_servers"] = allowed
    # Restart or stop MCP sessions based on new policy
    if mgr is not None:
        try:
            loop.run_until_complete(mgr.stop())
        except Exception:
            pass
        mgr = None
    if not no_tools:
        try:
            mgr = loop.run_until_complete(ensure_sessions_started_async(config))
        except Exception:
            mgr = None
    prompt_str = build_prompt(
        agent_name=agent_name,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        readline_enabled=readline_enabled,
    )
    return agent_conf, prompt_str, agent_name, no_tools, mgr


# Simple command registry and help text
_COMMANDS_USAGE = {
    "/exit": "Exit the REPL",
    "/quit": "Exit the REPL (alias)",
    "/model <name>": "Override the current model",
    "/agent <name>": "Switch to a configured agent",
    "/reasoning_effort [minimal|low|medium|high]": "Set reasoning effort for current agent",
    "/help": "Show available commands",
}


def get_command_names() -> List[str]:
    return ["/exit", "/quit", "/model", "/agent", "/reasoning_effort", "/help"]


def command_help() -> str:
    lines = ["Available commands:"]
    for cmd, desc in _COMMANDS_USAGE.items():
        lines.append(f"  {cmd:45} - {desc}")
    return "\n".join(lines)


def setup_readline(get_agent_names: Callable[[], List[str]]) -> Tuple[bool, Any]:
    """Configure GNU readline with a simple completer for REPL slash-commands.
    Returns (enabled, readline_module_or_None).
    """
    try:
        import readline as _readline  # type: ignore
    except Exception:
        return False, None
    try:
        _readline.parse_and_bind("tab: complete")
        try:
            delims = _readline.get_completer_delims()
            if "/" in delims:
                _readline.set_completer_delims(delims.replace("/", ""))
        except Exception:
            pass
        commands = get_command_names()

        def _completer(text, state):
            try:
                buf = _readline.get_line_buffer()
            except Exception:
                buf = ""
            if not buf.startswith("/"):
                return None
            parts = buf.strip().split()
            # complete command
            if len(parts) <= 1 and not buf.endswith(" "):
                opts = [c for c in commands if c.startswith(text or "")]
                return opts[state] if state < len(opts) else None
            cmd = parts[0]
            arg_prefix = ""
            try:
                arg_prefix = "" if buf.endswith(" ") else (text or "")
            except Exception:
                arg_prefix = text or ""
            if cmd == "/agent":
                names = []
                try:
                    names = list(get_agent_names() or [])
                except Exception:
                    names = []
                opts = [n for n in names if n.startswith(arg_prefix)]
                return opts[state] if state < len(opts) else None
            if cmd == "/reasoning_effort":
                opts = [o for o in ["minimal", "low", "medium", "high"] if o.startswith(arg_prefix)]
                return opts[state] if state < len(opts) else None
            if cmd == "/model":
                return None
            if cmd == "/help":
                return None
            return None

        try:
            _readline.set_completer(_completer)
        except Exception:
            pass
        return True, _readline
    except Exception:
        return False, None


def add_history(readline_module: Any, line: str) -> None:
    if readline_module is None:
        return
    try:
        readline_module.add_history(line)
    except Exception:
        pass
