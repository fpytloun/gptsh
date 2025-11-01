from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import click

from gptsh.core.agent import Agent
from gptsh.core.config_api import compute_tools_policy
from gptsh.core.exceptions import ReplExit
from gptsh.core.sessions import (
    load_session as _load_session,
    new_session_doc as _new_session_doc,
    resolve_session_ref as _resolve_session_ref,
    resolve_small_model as _resolve_small_model,
)
from gptsh.mcp import ensure_sessions_started_async as ensure_sessions_started_async  # noqa: F401

_log = logging.getLogger(__name__)


def build_prompt(
    *,
    agent_name: Optional[str],
    model: Optional[str],
    readline_enabled: bool,
) -> str:
    model_label = str(model or "?").rsplit("/", 1)[-1]
    agent_label = agent_name or "default"
    agent_col = click.style(agent_label, fg="cyan", bold=True)
    model_col = click.style(model_label, fg="magenta")
    return (
        re.sub("(\x1b\\[[0-9;]*[A-Za-z])", r"\001\1\002", f"{agent_col}|{model_col}> ")
        if readline_enabled
        else f"{agent_col}|{model_col}> "
    )


def command_exit() -> None:
    raise ReplExit()


def command_model(
    arg: Optional[str],
    *,
    agent: Agent,
    agent_name: Optional[str],
    readline_enabled: bool,
) -> Tuple[str, str]:
    if not arg:
        raise ValueError("Usage: /model <model>")
    new_model = arg.strip()
    agent.llm._base["model"] = new_model
    prompt_str = build_prompt(
        agent_name=agent_name,
        model=new_model,
        readline_enabled=readline_enabled,
    )
    return new_model, prompt_str


def command_reasoning_effort(arg: Optional[str], agent: Agent):
    if not arg:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    val = arg.strip().lower()
    if val not in {"minimal", "low", "medium", "high"}:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    agent.llm._base["reasoning_effort"] = val


def command_info(agent: Agent) -> str:
    """Return a human-readable session/model info string.

    Includes agent name, effective model, key parameters, usage (tokens/cost),
    and context window with usage percentage using litellm.get_max_tokens.
    """
    model = agent.llm._base.get("model", "?")

    # Pull session usage from CLI cache if available
    session = agent.session

    usage: Dict = {}
    if session:
        # Session might not be initialized yet
        usage = session.usage

    tokens = usage.get("tokens", {}) or {}
    prompt_t = tokens.get("prompt")
    completion_t = tokens.get("completion")
    total_t = tokens.get("total")
    cached_t = tokens.get("cached_tokens")
    reasoning_t = tokens.get("reasoning_tokens")
    cost = usage.get("cost")

    # Determine max context via litellm.get_max_tokens
    max_ctx = None
    try:
        from litellm.utils import _get_model_info_helper  # type: ignore

        info = _get_model_info_helper(model=model) or {}
        max_ctx = info.get("max_input_tokens")
    except Exception:
        max_ctx = None

    pct = None
    try:
        if isinstance(total_t, (int, float)) and isinstance(max_ctx, int) and max_ctx > 0:
            pct = (float(total_t) / float(max_ctx)) * 100.0
    except Exception:
        pct = None

    params_parts: List[str] = []
    tval = agent.llm._base.get("temperature")
    if tval is not None:
        params_parts.append(f"temperature={tval}")
    reff = agent.llm._base.get("reasoning_effort")
    if reff is not None:
        params_parts.append(f"reasoning_effort={reff}")
    params_str = (", ".join(params_parts)) if params_parts else "(default)"

    lines: List[str] = []
    lines.append(f"Model: {model}")
    lines.append(f"Parameters: {params_str}")
    if any(v is not None for v in [prompt_t, completion_t, reasoning_t, total_t, cached_t, cost]):
        lines.append("Session usage:")
        if prompt_t is not None:
            lines.append(f"  - prompt tokens: {prompt_t}")
        if completion_t is not None:
            lines.append(f"  - completion tokens: {completion_t}")
        if reasoning_t is not None:
            lines.append(f"  - reasoning tokens: {reasoning_t}")
        if total_t is not None:
            lines.append(f"  - total tokens: {total_t}")
        if cached_t is not None:
            lines.append(f"  - cached tokens: {cached_t}")
        if cost is not None:
            lines.append(f"  - total cost: ${cost:.5f}")
    else:
        lines.append("Usage: (no usage recorded yet in this session)")
    if max_ctx is not None:
        if pct is not None:
            lines.append(f"Context window: {total_t or 0} / {max_ctx} tokens (~{pct:.1f}%)")
        else:
            lines.append(f"Context window: {max_ctx} tokens")
    else:
        lines.append("Context window: (unknown)")

    return "\n".join(lines)


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
            name = getattr(h, "name", "?")
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
    if val == "on":
        effective_no_tools = True
    elif val == "off":
        effective_no_tools = False
    else:
        effective_no_tools = not current_no_tools
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
    agent_conf = agents_conf_all.get(new_agent) or {}
    agent_name = new_agent
    cli_model_override = agent_conf.get("model") if isinstance(agent_conf, dict) else None
    labels = None
    no_tools, allowed = compute_tools_policy(agent_conf, labels, False)
    mcp_cfg = config.setdefault("mcp", {})
    if allowed is not None:
        mcp_cfg["allowed_servers"] = allowed
    else:
        mcp_cfg.pop("allowed_servers", None)
    try:
        nonce = (mcp_cfg.get("_repl_nonce") or 0) + 1
        mcp_cfg["_repl_nonce"] = nonce
    except Exception as e:
        _log.debug("Failed to bump MCP nonce: %s", e)
        mcp_cfg["_repl_nonce"] = 1
    mgr = None
    prompt_str = build_prompt(
        agent_name=agent_name,
        model=cli_model_override,
        readline_enabled=readline_enabled,
    )
    return agent_conf, prompt_str, agent_name, no_tools, mgr


def command_file(arg: Optional[str], agent: Agent) -> str:
    """Read file and return content or marker to inject into conversation.

    - Small UTF-8 text files: return inline content with truncation notice if needed.
    - Binary or large files: return a concise marker.

    CRITICAL: Uses is_probably_text() to prevent any binary from being inlined.
    """
    if not arg:
        raise ValueError("Usage: /file <path>")

    from pathlib import Path

    from gptsh.core.stdin_handler import is_probably_text, sniff_mime

    path = Path(arg.strip()).expanduser()
    if not path.is_file():
        raise ValueError(f"File not found or not accessible: {path}")

    max_inline = 65536  # 64KB
    try:
        size = path.stat().st_size
        if size > max_inline:
            # Too large: return marker
            try:
                with open(path, "rb") as f:
                    head = f.read(512)
                mime = sniff_mime(head)
            except Exception:
                mime = "application/octet-stream"
            return f"[Attached file: {path.name} ({mime}, {size} bytes)]"

        # Read full content for analysis
        with open(path, "rb") as f:
            data = f.read(max_inline + 1)

        truncated = len(data) > max_inline
        if truncated:
            data = data[:max_inline]

        mime = sniff_mime(data)

        # CRITICAL: Only inline if MIME is text/plain AND passes safety check
        if mime == "text/plain" and is_probably_text(data):
            try:
                text = data.decode("utf-8", errors="strict")
                if truncated:
                    text += "\n[...File truncated. Content exceeded 64KB.]"
                return f"File: {path.name}\n{text}"
            except UnicodeDecodeError:
                # Should never happen since is_probably_text checks decode
                pass

        # Everything else: binary marker
        return f"[Attached file: {path.name} ({mime}, {size} bytes)]"

    except Exception as e:
        raise ValueError(f"Failed to read file: {e}") from e


# Simple command registry and help text
_COMMANDS_USAGE = {
    "/exit": "Exit the REPL",
    "/quit": "Exit the REPL (alias)",
    "/model <name>": "Override the current model",
    "/agent <name>": "Switch to a configured agent",
    "/reasoning_effort [minimal|low|medium|high]": "Set reasoning effort for current agent",
    "/tools": "List discovered MCP tools for current agent",
    "/no-tools [on|off]": "Toggle or set MCP tool usage for this session",
    "/info": "Show session/model info and usage",
    "/file <path>": "Attach a file to the conversation",
    "/help": "Show available commands",
    "/compact": "Summarize and compact conversation history",
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
        "/info",
        "/file",
        "/help",
        "/compact",
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
                _log.debug("readline: libedit detected")
                _readline.parse_and_bind("bind ^I rl_complete")
                _readline.parse_and_bind("bind ^R em-inc-search-prev")
                _readline.parse_and_bind("bind ^S em-inc-search-next")
            else:
                _readline.parse_and_bind("tab: complete")
                _readline.parse_and_bind('"\\C-r": reverse-search-history')
                _readline.parse_and_bind('"\\C-s": forward-search-history')
        except Exception as e:
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
    initial_user_message: Optional[Union[str, Dict[str, Any]]] = None,
    progress_reporter: Optional[Any] = None,
    session_ref: Optional[str] = None,
    sessions_enabled: Optional[bool] = None,
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
    console_err = Console(stderr=True)
    rl_enabled, rl = setup_readline(lambda: list((config.get("agents") or {}).keys()))

    model = (getattr(agent.llm, "_base", {}) or {}).get("model")
    agent_label = getattr(agent, "name", "default") or "default"
    cli_model_override: Optional[str] = model
    prompt_str = build_prompt(
        agent_name=agent_label,
        model=cli_model_override,
        readline_enabled=rl_enabled,
    )

    # Ensure progress reporter is initialized for REPL turns
    try:
        if progress_reporter is not None:
            progress_reporter.start()
    except Exception:
        pass

    try:
        no_tools = not any(len(v or []) > 0 for v in (agent.tools or {}).values())
    except Exception as e:
        _log.debug("Failed to inspect agent tools: %s", e)
        no_tools = True

    # Session history lives in ChatSession; no external history list
    last_interrupt = 0.0
    try:
        from gptsh.mcp.manager import MCPManager as _MCPManager
    except Exception:  # pragma: no cover - fallback
        _MCPManager = None  # type: ignore
    mcp_manager = None if no_tools or _MCPManager is None else _MCPManager(config)

    # Preload session if requested
    preloaded_doc: Optional[Dict[str, Any]] = None
    if session_ref and (sessions_enabled is None or sessions_enabled):
        try:
            sid = _resolve_session_ref(str(session_ref))
            preloaded_doc = _load_session(sid)
        except Exception as e:
            click.echo(f"Warning: failed to load referenced session '{session_ref}': {e}", err=True)
            preloaded_doc = None
    # Ensure agent.session exists and can be preloaded
    if getattr(agent, "session", None) is None:
        from gptsh.core.session import ChatSession as _ChatSession

        try:
            sess = _ChatSession.from_agent(
                agent,
                progress=progress_reporter,
                config=config,
                mcp=(None if no_tools else mcp_manager),
            )
            await sess.start()
            agent.session = sess
        except Exception as e:
            _log.debug("Failed to initialize ChatSession for REPL preload: %s", e)
    if (
        preloaded_doc
        and getattr(agent, "session", None) is not None
        and (sessions_enabled is None or sessions_enabled)
    ):
        from gptsh.core.sessions import preload_session_to_chat as _preload_chat

        _preload_chat(preloaded_doc, agent.session)

    async def _run_once(
        user_message: Union[str, Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        nonlocal preloaded_doc
        # Use the persistence-aware runner so titles are generated and turns are saved consistently
        from gptsh.core.runner import (
            RunRequest as _RunRequest,
            run_turn_with_persistence as _run_persist,
        )

        # Ensure a ChatSession exists on the agent (runner can also create it if needed)
        sess = getattr(agent, "session", None)

        # Prepare or reuse a session document when sessions are enabled
        doc = preloaded_doc
        msgs: List[Dict[str, Any]] = []  # not used by persistence runner, kept for signature

        # Resolve small model for title generation
        small_model = _resolve_small_model(
            (config.get("agents") or {}).get(agent_label) or {},
            (config.get("providers") or {}).get(config.get("default_provider")) or {},
        ) or (getattr(agent.llm, "_base", {}) or {}).get("model")

        # Create a new session doc if needed and sessions are enabled
        from gptsh.core.sessions import new_session_doc as __new_session_doc

        if doc is None and (sessions_enabled is None or sessions_enabled):
            chosen_model = (getattr(agent.llm, "_base", {}) or {}).get("model")
            agent_info = {
                "name": getattr(agent, "name", None),
                "model": chosen_model,
                "model_small": small_model or chosen_model,
                "prompt_system": (
                    (
                        (config.get("agents", {}) or {}).get(getattr(agent, "name", "default"), {})
                        or {}
                    ).get("prompt", {})
                    or {}
                ).get("system"),
                "params": {
                    k: v
                    for k, v in (getattr(agent, "generation_params", {}) or {}).items()
                    if k in {"temperature", "reasoning_effort"} and v is not None
                },
            }
            provider_info = {"name": (config.get("default_provider") or None)}
            doc = __new_session_doc(
                agent_info=agent_info,
                provider_info=provider_info,
                output=output_format,
                mcp_allowed_servers=(config.get("mcp", {}) or {}).get("allowed_servers"),
            )

        req = _RunRequest(
            agent=agent,
            user_message=user_message,
            config=config,
            stream=stream,
            output_format=output_format,
            no_tools=no_tools,
            logger=console,
            exit_on_interrupt=False,
            result_sink=None,  # runner prints to console directly
            messages_sink=None,  # persistence runner manages messages internally
            mcp_manager=mcp_manager,
            progress_reporter=progress_reporter,
            session=sess,
            session_doc=doc,
            small_model=small_model,
        )
        await _run_persist(req)
        # Persist updated doc reference for subsequent turns (same object mutated)
        preloaded_doc = doc
        return ("", msgs)

    while True:
        if initial_user_message:
            # Handle initial message (may be multimodal dict or string)
            if isinstance(initial_user_message, dict):
                # Multimodal message - run it and continue REPL
                await _run_once(initial_user_message)
                initial_user_message = None
                continue
            else:
                # Plain string - use as line
                line = initial_user_message
                initial_user_message = None
        else:
            try:
                doc = getattr(rl, "__doc__", "") or ""
                if "libedit" in doc.lower():
                    async with progress_reporter.aio_io():
                        click.echo(prompt_str, nl=False)
                        line = input()
                else:
                    async with progress_reporter.aio_io():
                        line = input(prompt_str)
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_interrupt <= 1.5:
                    click.echo("", err=True)
                    break
                last_interrupt = now
                async with progress_reporter.aio_io():
                    console_err.print("\n[grey50]Press Ctrl-C again to exit[/grey50]")
                continue
            except EOFError:
                async with progress_reporter.aio_io():
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
            if cmd == "/info":
                click.echo(command_info(agent))
                continue
            if cmd == "/model":
                try:
                    new_override, new_prompt = command_model(
                        arg,
                        agent=agent,
                        agent_name=agent_label,
                        readline_enabled=rl_enabled,
                    )
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                    continue
                cli_model_override = new_override
                prompt_str = new_prompt
                continue
            if cmd == "/reasoning_effort":
                try:
                    command_reasoning_effort(arg, agent)
                except ValueError as ve:
                    click.echo(str(ve), err=True)

                continue
            if cmd == "/agent":
                try:
                    loop = asyncio.get_running_loop()
                    agent_conf_out, prompt_out, agent_name_out, no_tools, _mgr = command_agent(
                        arg,
                        config=config,
                        agent_conf={},
                        agent_name=agent_label,
                        provider_conf={},
                        cli_model_override=cli_model_override,
                        no_tools=no_tools,
                        mgr=None,
                        loop=loop,
                        readline_enabled=rl_enabled,
                    )
                    from gptsh.core.config_resolver import build_agent as _build_agent

                    agent = await _build_agent(
                        config,
                        cli_agent=agent_name_out,
                        cli_provider=None,
                        cli_tools_filter=None,
                        cli_model_override=None,
                        cli_no_tools=no_tools,
                    )
                    agent_label = agent_name_out
                    model = getattr(agent.llm, "_base", {}).get("model")
                    cli_model_override = model
                    prompt_str = prompt_out
                    if _MCPManager is not None:
                        mcp_manager = None if no_tools else (_MCPManager(config))
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
                    if _MCPManager is not None:
                        mcp_manager = None if no_tools else (_MCPManager(config))
                except Exception as e:
                    _log.warning("Failed to toggle tools: %s", e)
                    click.echo(f"Failed to toggle tools: {e}", err=True)
                continue
            if cmd == "/file":
                try:
                    file_content = command_file(arg, agent)
                    # Append as a user message to the active session
                    sess = getattr(agent, "session", None)
                    if sess is not None:
                        sess.history.append({"role": "user", "content": file_content})
                        click.echo(f"File attached: {arg}")
                    else:
                        click.echo("No active session", err=True)
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                except Exception as e:
                    _log.warning("Failed to attach file: %s", e)
                    click.echo(f"Failed to attach file: {e}", err=True)
                continue
            if cmd == "/compact":
                sess = getattr(agent, "session", None)
                if sess is None:
                    click.echo("No active session", err=True)
                    continue
                # Resolve small model
                small_model = _resolve_small_model(
                    (config.get("agents") or {}).get(agent_label) or {},
                    (config.get("providers") or {}).get(config.get("default_provider")) or {},
                ) or (getattr(agent.llm, "_base", {}) or {}).get("model")
                summary = await sess.generate_summary(small_model=small_model)
                if not summary:
                    click.echo("No summary generated.")
                    continue
                # Preserve original system prompt; insert summary as first USER message and wipe rest
                hist = list(sess.history or [])
                new_hist: List[Dict[str, Any]] = []
                if hist and (hist[0].get("role") == "system"):
                    new_hist.append(hist[0])
                new_hist.append(
                    {
                        "role": "user",
                        "content": f"Summarized version of previous conversation:\n{summary}",
                    }
                )
                sess.history = new_hist
                # Print summary in current REPL format
                if output_format == "markdown":
                    try:
                        from rich.console import Console
                        from rich.markdown import Markdown

                        Console().print(Markdown(summary))
                    except Exception:
                        click.echo(summary)
                else:
                    click.echo(summary)
                continue
            click.echo("Unknown command", err=True)
            continue

        current_task = asyncio.create_task(_run_once(sline))
        try:
            result_text, msgs = await current_task
            # Persist turn to session store when sessions are enabled
            from gptsh.core.config_api import get_sessions_enabled

            if (
                get_sessions_enabled(
                    config,
                    agent_conf=(config.get("agents") or {}).get(agent_label),
                    no_sessions_cli=False,
                )
                if sessions_enabled is None
                else sessions_enabled
            ):
                try:
                    session_doc = preloaded_doc  # may be None initially
                except NameError:
                    session_doc = None
                sess = getattr(agent, "session", None)
                if session_doc is None:
                    chosen_model = (getattr(agent.llm, "_base", {}) or {}).get("model")
                    agent_info = {
                        "name": getattr(agent, "name", None),
                        "model": chosen_model,
                        "model_small": _resolve_small_model({}, {}) or chosen_model,
                        "prompt_system": (
                            (
                                (config.get("agents", {}) or {}).get(
                                    getattr(agent, "name", "default"), {}
                                )
                                or {}
                            ).get("prompt", {})
                            or {}
                        ).get("system"),
                        "params": {
                            k: v
                            for k, v in (getattr(agent, "generation_params", {}) or {}).items()
                            if k in {"temperature", "reasoning_effort"} and v is not None
                        },
                    }
                    provider_info = {"name": (config.get("default_provider") or None)}
                    session_doc = _new_session_doc(
                        agent_info=agent_info,
                        provider_info=provider_info,
                        output=output_format,
                        mcp_allowed_servers=(config.get("mcp", {}) or {}).get("allowed_servers"),
                    )
                from gptsh.core.sessions import save_after_turn as _save_after

                _save_after(session_doc, sess, msgs)
                preloaded_doc = session_doc
        except (KeyboardInterrupt, asyncio.CancelledError):
            current_task.cancel()
            try:
                await current_task
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            last_interrupt = time.monotonic()
            async with progress_reporter.aio_io():
                console_err.print("[grey50]Request cancelled[/grey50]")
            continue


def run_agent_repl(
    *,
    agent: Any,
    config: Dict[str, Any],
    output_format: str,
    stream: bool,
    initial_user_message: Optional[Union[str, Dict[str, Any]]] = None,
    progress_reporter: Optional[Any] = None,
    session_ref: Optional[str] = None,
    sessions_enabled: Optional[bool] = None,
) -> None:
    asyncio.run(
        run_agent_repl_async(
            agent=agent,
            config=config,
            output_format=output_format,
            stream=stream,
            initial_user_message=initial_user_message,
            progress_reporter=progress_reporter,
            session_ref=session_ref,
            sessions_enabled=sessions_enabled,
        )
    )
