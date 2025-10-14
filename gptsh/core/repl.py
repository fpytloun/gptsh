from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import click

from gptsh.core.config_api import compute_tools_policy
from gptsh.core.exceptions import ReplExit
from gptsh.mcp import ensure_sessions_started_async as ensure_sessions_started_async  # noqa: F401


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


_log = logging.getLogger(__name__)


def command_tools(agent: Any) -> str:
    """Return a formatted list of tools for the current agent.

    Output matches the CLI list format: server (count):\n  - tool
    """
    tools_map = getattr(agent, "tools", {}) or {}
    if not tools_map:
        return "(no tools discovered)"
    lines: List[str] = []
    policy = getattr(agent, "policy", None)
    for server, handles in tools_map.items():
        lines.append(f"{server} ({len(handles)}):")
        for h in handles:
            name = getattr(h, 'name', '?')
            badge = ""
            try:
                if policy and policy.is_auto_allowed(server, name):
                    badge = " \u2714"  # checkmark for auto-approved
            except Exception as e:
                _log.debug("policy.is_auto_allowed failed for %s/%s: %s", server, name, e)
            lines.append(f"  - {name}{badge}")
    return "\n".join(lines)


def command_no_tools(
    arg: Optional[str],
    *,
    config: Dict[str, Any],
    agent_name: str,
    cli_model_override: Optional[str],
    current_no_tools: bool,
) -> tuple[Any, bool, str]:
    """Toggle or set no-tools and return (new_agent, no_tools, message).

    - arg: "on" to disable tools, "off" to enable tools, None/"" to toggle.
    - Rebuilds the Agent via build_agent to reflect the new policy.
    """
    val = (arg or "").strip().lower()
    if val not in {"", "on", "off"}:
        raise ValueError("Usage: /no-tools [on|off]")
    # Infer current no_tools from agent.tools at call sites, then toggle here by caller's decision.
    # This function computes only new_no_tools based on desired val.
    # Determine effective state: toggle if no explicit value
    if val == "on":
        effective_no_tools = True
    elif val == "off":
        effective_no_tools = False
    else:
        effective_no_tools = not current_no_tools
    # Use a fresh loop to avoid nested run() issues under pytest-asyncio
    # Run coroutine in a dedicated thread to avoid interfering with any running loop
    import threading

    from gptsh.core.config_resolver import build_agent as _build_agent

    result_box: Dict[str, Any] = {}

    def _worker():  # pragma: no cover - thread setup
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_box["agent"] = loop.run_until_complete(
                _build_agent(
                    config,
                    cli_agent=agent_name,
                    cli_provider=None,
                    cli_tools_filter=None,
                    cli_model_override=cli_model_override,
                    cli_no_tools=effective_no_tools,
                )
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    new_agent = result_box.get("agent")
    tools_map = getattr(new_agent, "tools", {}) or {}
    msg = f"Tools {'disabled' if effective_no_tools else 'enabled'} ({sum(len(v or []) for v in tools_map.values())} available)"
    return new_agent, effective_no_tools, msg


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
    # Recompute fresh; do not treat previous no_tools as a CLI override when switching
    no_tools, allowed = compute_tools_policy(agent_conf, labels, False)
    mcp_cfg = config.setdefault("mcp", {})
    if allowed is not None:
        mcp_cfg["allowed_servers"] = allowed
    else:
        # Clear any previous filter so full set is available
        mcp_cfg.pop("allowed_servers", None)
    # Stop all existing MCP sessions so that subsequent discovery uses the updated policy/servers
    # Force a new manager key by toggling a nonce; this avoids reusing cached sessions
    try:
        nonce = (mcp_cfg.get("_repl_nonce") or 0) + 1
        mcp_cfg["_repl_nonce"] = nonce
    except Exception as e:
        _log.debug("Failed to bump MCP nonce: %s", e)
        mcp_cfg["_repl_nonce"] = 1
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
    "/tools": "List discovered MCP tools for current agent",
    "/no-tools [on|off]": "Toggle or set MCP tool usage for this session",
    "/help": "Show available commands",
}


def get_command_names() -> List[str]:
    return [
        "/exit",
        "/quit",
        "/model",
        "/agent",
        "/reasoning_effort",
        "/tools",
        "/no-tools",
        "/help",
    ]


def command_help() -> str:
    lines = ["Available commands:"]
    for cmd, desc in _COMMANDS_USAGE.items():
        lines.append(f"  {cmd:45} - {desc}")
    return "\n".join(lines)


def setup_readline(get_agent_names: Callable[[], List[str]]) -> Tuple[bool, Any]:
    """Configure readline/libedit with a simple completer for REPL slash-commands.
    Returns (enabled, readline_module_or_None).

    Notes:
    - On macOS Python is often linked against libedit instead of GNU readline.
      In that case the correct binding for tab completion is
      "bind ^I rl_complete" instead of "tab: complete".
    """
    try:
        try:
            import gnureadline as _readline  # type: ignore
        except ImportError:
            import readline as _readline  # type: ignore
    except Exception as e:
        _log.warning("readline import failed: %s", e)
        return False, None
    try:
        try:
            doc = getattr(_readline, "__doc__", "") or ""
            if "libedit" in doc.lower():
                print("libedit")
                # macOS/libedit: different binding syntax for tab completion
                _readline.parse_and_bind("bind ^I rl_complete")
                # Enable incremental reverse search on Ctrl-R (and forward on Ctrl-S)
                _readline.parse_and_bind("bind ^R em-inc-search-prev")
                _readline.parse_and_bind("bind ^S em-inc-search-next")
            else:
                # GNU readline
                _readline.parse_and_bind("tab: complete")
                # Enable reverse/forward history search
                _readline.parse_and_bind('"\\C-r": reverse-search-history')
                _readline.parse_and_bind('"\\C-s": forward-search-history')
        except Exception as e:
            # Best-effort: log and continue to attempt to set completer
            _log.debug("readline parse_and_bind failed: %s", e)
        try:
            delims = _readline.get_completer_delims()
            if "/" in delims:
                _readline.set_completer_delims(delims.replace("/", ""))
        except Exception as e:
            _log.debug("failed to adjust completer delimiters: %s", e)
        commands = get_command_names()

        def _completer(text, state):
            try:
                buf = _readline.get_line_buffer()
            except Exception as e:
                _log.debug("readline.get_line_buffer failed: %s", e)
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
            except Exception as e:
                _log.debug("computing arg_prefix failed: %s", e)
                arg_prefix = text or ""
            if cmd == "/agent":
                names = []
                try:
                    names = list(get_agent_names() or [])
                except Exception as e:
                    _log.debug("get_agent_names failed: %s", e)
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
        except Exception as e:
            _log.debug("set_completer failed: %s", e)
        return True, _readline
    except Exception as e:
        _log.warning("setup_readline failed: %s", e)
        return False, None


def add_history(readline_module: Any, line: str) -> None:
    if readline_module is None:
        return
    try:
        readline_module.add_history(line)
    except Exception as e:
        _log.debug("add_history failed: %s", e)




async def run_agent_repl_async(
    *,
    agent: Any,
    config: Dict[str, Any],
    output_format: str,
    stream: bool,
    progress: bool,
    initial_prompt: Optional[str] = None,
) -> None:
    """Interactive REPL loop using only a resolved Agent.

    - Displays a simple prompt "<agent>|<model>>".
    - On each turn executes the prompt with the agent (streaming or non-streaming).
    - Maintains a simple in-memory history for the current session.
    - Supports /help and /exit.
    """
    import time

    import click
    from rich.console import Console


    console = Console()
    # Readline for history/convenience, provide agent names for completion
    rl_enabled, rl = setup_readline(lambda: list((config.get("agents") or {}).keys()))

    model = (getattr(agent.llm, "_base", {}) or {}).get("model")
    # model_label computed by build_prompt
    agent_label = getattr(agent, "name", "default") or "default"
    provider_conf_local: Dict[str, Any] = dict(getattr(agent, "provider_conf", {}) or {})
    agent_conf_local: Dict[str, Any] = dict(getattr(agent, "agent_conf", {}) or {})
    cli_model_override: Optional[str] = model or provider_conf_local.get("model")
    prompt_str = build_prompt(
        agent_name=agent_label,
        provider_conf=provider_conf_local,
        agent_conf=agent_conf_local,
        cli_model_override=cli_model_override,
        readline_enabled=rl_enabled,
    )

    # Heuristic: if agent has no tools, disable tools in non-stream flow
    try:
        no_tools = not any(len(v or []) > 0 for v in (agent.tools or {}).values())
    except Exception as e:
        _log.debug("Failed to inspect agent tools: %s", e)
        no_tools = True

    # Keep a heuristic no_tools flag based on resolved agent tools

    history_messages: List[Dict[str, Any]] = []
    last_interrupt = 0.0

    async def _run_once(user_text: str) -> str:
        # Reuse CLI run_llm to ensure consistent streaming fallback and tool behavior
        from gptsh.cli.entrypoint import run_llm as _run_llm
        sink: List[str] = []
        await _run_llm(
            prompt=user_text,
            provider_conf=provider_conf_local,
            agent_conf=agent_conf_local,
            cli_model_override=cli_model_override,
            stream=stream,
            progress=progress,
            output_format=output_format,
            no_tools=no_tools,
            config=config,
            logger=console,
            history_messages=history_messages,
            result_sink=sink,
            agent_obj=agent,
        )
        return (sink[0] if sink else "")

    while True:
        if initial_prompt:
            line = initial_prompt
            initial_prompt = None
        else:
            try:
                doc = getattr(rl, "__doc__", "") or ""
                if "libedit" in doc.lower():
                    # MacOS non-GNU readline compatibility
                    click.echo(prompt_str, nl=False)
                    line = input()
                else:
                    line = input(prompt_str)
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_interrupt <= 1.5:
                    click.echo("", err=True)
                    break
                last_interrupt = now
                click.echo("(^C) Press Ctrl-C again to exit", err=True)
                continue
            except EOFError:
                click.echo("", err=True)
                break

        sline = line.strip()
        if not sline:
            continue

        add_history(rl, sline)

        if sline.startswith("/"):
            parts = sline.split(None, 1)
            cmd = parts[0]
            arg = parts[1] if len(parts) == 2 else None
            if cmd in ("/exit", "/quit"):
                click.echo("", err=True)
                break
            if cmd == "/help":
                click.echo(command_help())
                continue
            if cmd == "/model":
                try:
                    new_override, new_prompt = command_model(
                        arg,
                        agent_conf=agent_conf_local,
                        provider_conf=provider_conf_local,
                        cli_model_override=cli_model_override,
                        agent_name=agent_label,
                        readline_enabled=rl_enabled,
                    )
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                    continue
                cli_model_override = new_override
                try:
                    agent.llm._base["model"] = cli_model_override
                except Exception as e:
                    _log.debug("Failed to update agent base model: %s", e)
                provider_conf_local["model"] = cli_model_override
                prompt_str = new_prompt
                continue
            if cmd == "/reasoning_effort":
                try:
                    agent_conf_local = command_reasoning_effort(arg, agent_conf_local)
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                continue
            if cmd == "/agent":
                try:
                    # Reuse existing helper to produce consistent prompt and policy
                    loop = asyncio.get_running_loop()
                    agent_conf_out, prompt_out, agent_name_out, no_tools, _mgr = command_agent(
                        arg,
                        config=config,
                        agent_conf=agent_conf_local,
                        agent_name=agent_label,
                        provider_conf=provider_conf_local,
                        cli_model_override=cli_model_override,
                        no_tools=no_tools,
                        mgr=None,
                        loop=loop,
                        readline_enabled=rl_enabled,
                    )
                    # Rebuild Agent to reflect new selection using current loop
                    from gptsh.core.config_resolver import build_agent as _build_agent
                    agent = await _build_agent(
                        config,
                        cli_agent=agent_name_out,
                        cli_provider=None,
                        cli_tools_filter=None,
                        cli_model_override=None,
                        cli_no_tools=no_tools,
                    )
                    agent_conf_local = agent_conf_out if isinstance(agent_conf_out, dict) else {}
                    agent_label = agent_name_out
                    model = getattr(agent.llm, "_base", {}).get("model")
                    cli_model_override = model
                    provider_conf_local["model"] = model
                    prompt_str = prompt_out
                except Exception as e:
                    _log.warning("Failed to switch agent: %s", e)
                    click.echo(f"Failed to switch agent: {e}", err=True)
                continue
            if cmd == "/tools":
                try:
                    click.echo(command_tools(agent))
                except Exception as e:
                    _log.warning("Failed to list tools: %s", e)
                    click.echo(f"Failed to list tools: {e}", err=True)
                continue
            if cmd == "/no-tools":
                try:
                    # Toggle or set explicitly
                    desired = (arg or "").strip().lower()
                    if desired not in {"", "on", "off"}:
                        click.echo("Usage: /no-tools [on|off]", err=True)
                        continue
                    new_agent, _no, msg = command_no_tools(
                        desired,
                        config=config,
                        agent_name=agent_label,
                        cli_model_override=cli_model_override,
                        current_no_tools=no_tools,
                    )
                    agent = new_agent
                    no_tools = _no
                    click.echo(msg)
                except Exception as e:
                    _log.warning("Failed to toggle tools: %s", e)
                    click.echo(f"Failed to toggle tools: {e}", err=True)
                continue
            click.echo("Unknown command", err=True)
            continue

        try:
            user_msg = {"role": "user", "content": sline}
            content = await _run_once(sline)
            history_messages.extend([user_msg, {"role": "assistant", "content": content}])
        except KeyboardInterrupt:
            last_interrupt = time.monotonic()
            click.echo("Cancelled.", err=True)
            continue


def run_agent_repl(
    *,
    agent: Any,
    config: Dict[str, Any],
    output_format: str,
    stream: bool,
    progress: bool,
    initial_prompt: Optional[str] = None,
) -> None:
    asyncio.run(
        run_agent_repl_async(
            agent=agent,
            config=config,
            output_format=output_format,
            stream=stream,
            progress=progress,
            initial_prompt=initial_prompt,
        )
    )
