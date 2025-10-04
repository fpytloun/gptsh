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
            except Exception:
                pass
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

    import asyncio

    from gptsh.core.config_resolver import build_agent as _build_agent
    # Use a fresh loop to avoid nested run() issues under pytest-asyncio
    # Run coroutine in a dedicated thread to avoid interfering with any running loop
    import threading

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


def run_agent_repl(
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
    import asyncio
    import sys
    import time

    import click
    from rich.console import Console
    from rich.markdown import Markdown

    from gptsh.core.api import run_prompt_with_agent
    from gptsh.core.progress import RichProgressReporter
    from gptsh.core.session import ChatSession

    console = Console()
    # Readline for history/convenience, provide agent names for completion
    rl_enabled, rl = setup_readline(lambda: list((config.get("agents") or {}).keys()))

    model = (getattr(agent.llm, "_base", {}) or {}).get("model")
    model_label = str(model or (agent.provider_conf or {}).get("model") or "?").rsplit("/", 1)[-1]
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
    except Exception:
        no_tools = True

    # Keep a heuristic no_tools flag based on resolved agent tools

    history_messages: List[Dict[str, Any]] = []
    last_interrupt = 0.0

    async def _run_once(user_text: str) -> str:
        pr: Optional[RichProgressReporter] = None
        waiting_id = None
        content = ""
        try:
            if progress and sys.stderr.isatty():
                pr = RichProgressReporter()
                pr.start()
            if stream:
                session = ChatSession.from_agent(agent, progress=pr, config=config)
                params, chosen_model = await session.prepare_stream(
                    prompt=user_text,
                    provider_conf={},
                    agent_conf=agent_conf_local,
                    cli_model_override=cli_model_override,
                    history_messages=history_messages,
                )
                wait_label = f"Waiting for {str(chosen_model).rsplit('/', 1)[-1]}"
                if pr is not None:
                    waiting_id = pr.add_task(wait_label)
                md_buffer = "" if output_format == "markdown" else ""
                async for chunk in session.stream_with_params(params):
                    if not chunk:
                        continue
                    if pr is not None and waiting_id is not None:
                        pr.complete_task(waiting_id)
                        waiting_id = None
                        try:
                            pr.stop()
                        except Exception:
                            pass
                    if output_format == "markdown":
                        md_buffer += chunk
                        while "\n" in md_buffer:
                            line, md_buffer = md_buffer.split("\n", 1)
                            console.print(Markdown(line))
                    else:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    content += chunk
                if output_format == "markdown" and md_buffer:
                    console.print(Markdown(md_buffer))
                else:
                    click.echo()
            else:
                wait_label = f"Waiting for {model_label}"
                if pr is not None:
                    waiting_id = pr.add_task(wait_label)
                content = await run_prompt_with_agent(
                    agent=agent,
                    prompt=user_text,
                    config=config,
                    provider_conf={},
                    agent_conf=agent_conf_local,
                    cli_model_override=cli_model_override,
                    no_tools=no_tools,
                    history_messages=history_messages,
                    progress_reporter=pr,
                )
                if output_format == "markdown":
                    console.print(Markdown(content or ""))
                else:
                    click.echo(content or "")
        finally:
            if pr is not None:
                if waiting_id is not None:
                    pr.complete_task(waiting_id)
                try:
                    pr.stop()
                except Exception:
                    pass
        return content or ""

    if initial_prompt and str(initial_prompt).strip():
        try:
            user_msg = {"role": "user", "content": initial_prompt}
            content = asyncio.run(_run_once(initial_prompt))
            history_messages.extend([user_msg, {"role": "assistant", "content": content}])
        except KeyboardInterrupt:
            last_interrupt = time.monotonic()
            click.echo("Cancelled.", err=True)

    while True:
        try:
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
                except Exception:
                    pass
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
                    loop = asyncio.new_event_loop()
                    try:
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
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass
                    # Rebuild Agent to reflect new selection
                    from gptsh.core.config_resolver import build_agent as _build_agent
                    agent = asyncio.run(
                        _build_agent(
                            config,
                            cli_agent=agent_name_out,
                            cli_provider=None,
                            cli_tools_filter=None,
                            cli_model_override=None,
                            cli_no_tools=no_tools,
                        )
                    )
                    agent_conf_local = agent_conf_out if isinstance(agent_conf_out, dict) else {}
                    agent_label = agent_name_out
                    model = getattr(agent.llm, "_base", {}).get("model")
                    cli_model_override = model
                    provider_conf_local["model"] = model
                    prompt_str = prompt_out
                except Exception as e:
                    click.echo(f"Failed to switch agent: {e}", err=True)
                continue
            if cmd == "/tools":
                try:
                    click.echo(command_tools(agent))
                except Exception as e:
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
                    click.echo(f"Failed to toggle tools: {e}", err=True)
                continue
            click.echo("Unknown command", err=True)
            continue

        add_history(rl, sline)
        try:
            user_msg = {"role": "user", "content": sline}
            content = asyncio.run(_run_once(sline))
            history_messages.extend([user_msg, {"role": "assistant", "content": content}])
        except KeyboardInterrupt:
            last_interrupt = time.monotonic()
            click.echo("Cancelled.", err=True)
            continue
