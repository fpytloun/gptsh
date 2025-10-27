import asyncio
import os
import sys
import warnings

import click

from gptsh.cli.repl import run_agent_repl  # type: ignore
from gptsh.cli.utils import (
    is_tty as _is_tty,
    print_agents_listing as _print_agents_listing,
    print_tools_listing as _print_tools_listing,
    resolve_agent_and_settings as _resolve_agent_and_settings,
)
from gptsh.config.loader import load_config
from gptsh.core.config_resolver import build_agent
from gptsh.core.logging import setup_logging
from gptsh.core.runner import RunRequest, run_turn_with_request, run_turn_with_persistence
from gptsh.core.stdin_handler import read_stdin
from gptsh.mcp.api import get_auto_approved_tools, list_tools
from gptsh.mcp.manager import MCPManager
from gptsh.core.sessions import (
    list_sessions as _list_saved_sessions,
    resolve_session_ref as _resolve_session_ref,
    load_session as _load_session,
    save_session as _save_session,
    new_session_doc as _new_session_doc,
    append_messages as _append_session_messages,
    resolve_small_model as _resolve_small_model,
    generate_title as _generate_title,
)

# Ensure LiteLLM async HTTPX clients are closed cleanly on loop shutdown
try:
    from litellm.llms.custom_httpx.async_client_cleanup import (
        close_litellm_async_clients,  # type: ignore
    )
except Exception:
    close_litellm_async_clients = None  # type: ignore

from typing import Any, Dict, List, Optional

# Suppress known LiteLLM RuntimeWarning about un-awaited coroutine on loop close.
warnings.filterwarnings(
    "ignore",
    message=r".*coroutine 'close_litellm_async_clients' was never awaited.*",
    category=RuntimeWarning,
)

DEFAULT_AGENTS = {"default": {}}


# --- CLI Entrypoint ---


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--provider", default=None, help="Override LiteLLM provider from config")
@click.option("-m", "--model", default=None, help="Override LLM model")
@click.option("-a", "--agent", default=None, help="Named agent preset from config")
@click.option("-c", "--config", "config_path", default=None, help="Specify alternate config path")
@click.option("--stream/--no-stream", default=True)
@click.option("--progress/--no-progress", default=True)
@click.option("--debug", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging (INFO)")
@click.option(
    "--mcp-servers", "mcp_servers", default=None, help="Override path to MCP servers file"
)
@click.option("--list-tools", "list_tools_flag", is_flag=True, default=False)
@click.option(
    "--list-providers",
    "list_providers_flag",
    is_flag=True,
    default=False,
    help="List configured providers",
)
@click.option(
    "--list-agents",
    "list_agents_flag",
    is_flag=True,
    default=False,
    help="List configured agents and their tools",
)
@click.option(
    "--list-sessions",
    "list_sessions_flag",
    is_flag=True,
    default=False,
    help="List saved sessions",
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["text", "markdown", "default"]),
    default="default",
    help="Output format",
)
@click.option(
    "--no-tools", is_flag=True, default=False, help="Disable MCP tools (discovery and execution)"
)
@click.option(
    "--tools",
    "tools_filter",
    default=None,
    help="Comma/space-separated MCP server labels to allow (others skipped)",
)
@click.option(
    "--interactive", "-i", is_flag=True, default=False, help="Run in interactive REPL mode"
)
@click.option(
    "-s", "--session", "session_ref", default=None, help="Session reference (index or id)"
)
@click.option(
    "--no-sessions",
    "no_sessions",
    is_flag=True,
    default=False,
    help="Disable saving/loading conversation sessions",
)
@click.option("--assume-tty", is_flag=True, default=False, help="Assume TTY (for tests/CI)")
@click.argument("prompt", required=False)
def main(
    provider,
    model,
    agent,
    config_path,
    stream,
    progress,
    debug,
    verbose,
    mcp_servers,
    list_tools_flag,
    list_providers_flag,
    list_agents_flag,
    list_sessions_flag,
    output,
    no_tools,
    tools_filter,
    interactive,
    session_ref,
    no_sessions,
    assume_tty,
    prompt,
):
    """gptsh: Modular shell/LLM agent client."""
    # Restore default SIGINT handler to let REPL manage interrupts
    import signal

    signal.signal(signal.SIGINT, signal.default_int_handler)
    # Load config
    # Load configuration: use custom path or defaults
    if config_path:
        # Fail fast if the provided config path does not exist
        if not os.path.isfile(config_path):
            click.echo(f"Configuration file not found: {config_path}")
            sys.exit(2)
        try:
            config = load_config([config_path])
        except Exception as e:
            click.echo(f"Failed to load configuration from {config_path}: {e}")
            sys.exit(2)
    else:
        try:
            config = load_config()
        except Exception as e:
            click.echo(f"Failed to load configuration: {e}")
            sys.exit(2)

    if not _is_tty(stream="stderr"):
        # If stderr is not a tty, disable progress bar
        progress = False

    if mcp_servers:
        # Allow comma or whitespace-separated list of paths
        parts = [p for raw in mcp_servers.split(",") for p in raw.split() if p]
        # Validate that at least one provided servers file exists
        existing = [p for p in parts if os.path.isfile(os.path.expanduser(p))]
        if not existing:
            click.echo(f"MCP servers file(s) not found: {', '.join(parts) if parts else '(none)'}")
            sys.exit(2)
        mcp_cfg = config.setdefault("mcp", {})
        # If inline mcp.servers is configured, prefer it and ignore CLI file override
        if not mcp_cfg.get("servers"):
            # Mark CLI-provided paths so they are preferred among files
            mcp_cfg["servers_files_cli"] = parts if parts else []
            # Also set legacy key for compatibility in other code paths
            mcp_cfg["servers_files"] = parts if parts else []
    # Pre-parse CLI tools filter into list to later apply via config_api
    tools_filter_labels = None
    if tools_filter:
        tools_filter_labels = [p for raw in tools_filter.split(",") for p in raw.split() if p]
    # Logging: default WARNING, -v/--verbose -> INFO, --debug -> DEBUG
    log_level = "DEBUG" if debug else ("INFO" if verbose else "WARNING")
    log_fmt = config.get("logging", {}).get("format", "text")
    logger = setup_logging(log_level, log_fmt)

    # Merge default agent so it's always present for checks and later listing
    existing_agents = dict(config.get("agents") or {})
    config["agents"] = {**DEFAULT_AGENTS, **existing_agents}

    # Validate agent and provider names if explicitly set
    if agent and agent not in config.get("agents", {}):
        click.echo(f"Agent not found: {agent}")
        sys.exit(2)
    if provider and provider not in (config.get("providers") or {}):
        click.echo(f"Provider not found: {provider}")
        sys.exit(2)

    # Handle immediate listing flags
    if list_tools_flag:
        if no_tools:
            click.echo("MCP tools disabled by --no-tools")
            sys.exit(0)
        labels = None
        if tools_filter:
            labels = [p for raw in tools_filter.split(",") for p in raw.split() if p]
        # Build a minimal agent object for listing without requiring providers to be fully configured
        try:
            agent_obj = asyncio.run(
                build_agent(
                    config,
                    cli_agent=agent,
                    cli_provider=provider,
                    cli_tools_filter=labels,
                    cli_model_override=model,
                )
            )
        except Exception as e:
            # Surface configuration errors directly
            from gptsh.core.exceptions import ConfigError

            if isinstance(e, ConfigError):
                click.echo(f"Configuration error: {e}")
                sys.exit(2)
            # Fallback to direct MCP listing if agent resolution fails (e.g., no providers in stub tests)
            tools = list_tools(config)
            _print_tools_listing(tools, get_auto_approved_tools(config))
            sys.exit(0)
        if agent_obj is None:
            click.echo("Failed to resolve agent/tools")
            sys.exit(1)
        approved_map = get_auto_approved_tools(
            config,
            agent_conf=(config.get("agents") or {}).get(
                agent or (config.get("default_agent") or "default")
            ),
        )
        tools_map = {
            srv: [h.name for h in (agent_obj.tools or {}).get(srv, [])]
            for srv in (agent_obj.tools or {})
        }
        _print_tools_listing(tools_map, approved_map)
        sys.exit(0)

    if list_providers_flag:
        providers = config.get("providers", {})
        click.echo("Configured providers:")
        for name in providers:
            click.echo(f"  - {name}")
        sys.exit(0)

    if list_agents_flag:
        # Merge default agent so it's always listed
        existing_agents = dict(config.get("agents") or {})
        agents_conf = {**DEFAULT_AGENTS, **existing_agents}
        if not agents_conf:
            click.echo("No agents configured.")
            sys.exit(0)

        tools_map = {} if no_tools else (list_tools(config) or {})
        _print_agents_listing(config, agents_conf, tools_map, no_tools)
        sys.exit(0)

    if list_sessions_flag:
        from datetime import datetime

        def _fmt_local(iso: str) -> str:
            try:
                # Accept 'Z' suffix
                s = iso.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                return dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                return iso

        sessions = _list_saved_sessions()
        total = len(sessions)
        to_show = sessions[:20]
        # Align index column by computing width from last index shown
        idx_width = len(str(len(to_show) - 1)) if to_show else 1
        for idx, s in enumerate(to_show):
            idx_str = str(idx).rjust(idx_width)
            dt = s.get("updated_at") or s.get("created_at") or ""
            title = s.get("title") or "(untitled)"
            agent_name = s.get("agent") or "?"
            model_name = s.get("model") or "?"
            idx_part = (
                click.style("[", fg="bright_black")
                + click.style(f"{idx_str}", fg="bright_yellow")
                + click.style("]", fg="bright_black")
            )
            id_part = click.style(str(s.get("id")), fg="yellow")
            dt_part = click.style(_fmt_local(dt), fg="cyan")
            title_part = click.style(title, fg="green")
            agent_model_part = click.style(f"({agent_name}|{model_name})", fg="bright_black")
            click.echo(f"{idx_part} {id_part} {dt_part} {title_part} {agent_model_part}")
        remaining = total - len(to_show)
        if remaining > 10:
            click.echo(
                click.style(
                    f"[ {remaining} older sessions not shown ]", fg="bright_black"
                )
            )
        sys.exit(0)

    # If resuming a session, preload its agent/provider/model preferences unless overridden via CLI
    resume_doc = None
    if session_ref:
        try:
            sid = _resolve_session_ref(str(session_ref))
            resume_doc = _load_session(sid)
        except Exception as e:
            # Fallback to defaults: warn and proceed
            click.echo(f"Warning: failed to load referenced session '{session_ref}': {e}", err=True)
            resume_doc = None
    resume_agent = agent
    resume_provider = provider
    resume_model = model
    if resume_doc is not None:
        try:
            if resume_agent is None:
                resume_agent = (resume_doc.get("agent") or {}).get("name")
            if resume_provider is None:
                rp = (resume_doc.get("provider") or {}).get("name")
                if rp:
                    resume_provider = rp
            if resume_model is None:
                rm = (resume_doc.get("agent") or {}).get("model")
                if rm:
                    resume_model = rm
        except Exception:
            pass

    # Validate resumed agent/provider exist; warn and fall back if missing
    try:
        agents_conf = config.get("agents", {}) or {}
        providers_conf = config.get("providers", {}) or {}
        if resume_doc is not None:
            if resume_agent is None:
                cand = (resume_doc.get("agent") or {}).get("name")
                if isinstance(cand, str) and cand:
                    if cand in agents_conf:
                        resume_agent = cand
                    else:
                        click.echo(
                            f"Warning: agent '{cand}' not available, falling back to default agent",
                            err=True,
                        )
            if resume_provider is None:
                candp = (resume_doc.get("provider") or {}).get("name")
                if isinstance(candp, str) and candp:
                    if candp in providers_conf:
                        resume_provider = candp
                    else:
                        dp = config.get("default_provider") or (
                            next(iter(providers_conf)) if providers_conf else None
                        )
                        click.echo(
                            f"Warning: provider '{candp}' not available, falling back to default provider {dp}",
                            err=True,
                        )
    except Exception:
        pass

    # Resolve agent with safe fallback if stored values are invalid
    try:
        agent_obj, agent_conf, provider_conf, output_effective, no_tools_effective, _ = asyncio.run(
            _resolve_agent_and_settings(
                config=config,
                agent_name=resume_agent,
                provider_name=resume_provider,
                model_override=resume_model,
                tools_filter_labels=tools_filter_labels,
                no_tools_flag=no_tools,
                output_format=output,
            )
        )
    except KeyError as e:
        click.echo(f"Warning: {e}. Falling back to defaults.", err=True)
        agent_obj, agent_conf, provider_conf, output_effective, no_tools_effective, _ = asyncio.run(
            _resolve_agent_and_settings(
                config=config,
                agent_name=None,
                provider_name=None,
                model_override=None,
                tools_filter_labels=tools_filter_labels,
                no_tools_flag=no_tools,
                output_format=output,
            )
        )

    # Initial prompt from arg and/or stdin
    stdin_input = None
    if not _is_tty(stream="stdin"):
        # Stdin is not TTY so read stdin first
        try:
            stdin_input = read_stdin()
        except UnicodeDecodeError as e:
            raise click.ClickException(
                "Failed to decode input. Please ensure UTF-8 encoding."
            ) from e

        # We consumed something from stdin and have tty on stderr so session seems interactive, open /dev/tty for interactive inputs (tool approvals)
        if stdin_input and _is_tty(stream="stderr"):
            try:
                sys.stdin = open("/dev/tty", "r", encoding="utf-8", errors="replace")
            except OSError:
                # We cannot re-open stdin so assume session is not interactive
                pass

    # Construct prompt
    prompt = prompt or agent_conf.get("prompt", {}).get("user")
    initial_prompt = (
        f"{prompt}\n\n---\nInput:\n{stdin_input}"
        if (prompt and stdin_input)
        else (prompt or stdin_input)
    )

    # Initialize a single ProgressReporter for the REPL session and pass it down
    from gptsh.core.progress import NoOpProgressReporter, RichProgressReporter

    reporter = (
        RichProgressReporter(transient=True)
        if progress and _is_tty(stream="stderr")
        else NoOpProgressReporter()
    )

    # Interactive REPL mode
    if interactive:
        if not (
            _is_tty(assume_tty=assume_tty, stream="stdout")
            and _is_tty(assume_tty=assume_tty, stream="stdin")
        ):
            raise click.ClickException("Interactive mode requires a TTY.")

        try:
            # Hand off to agent-only REPL
            run_agent_repl(
                agent=agent_obj,
                config=config,
                output_format=output_effective,
                stream=stream,
                initial_prompt=initial_prompt,
                progress_reporter=reporter,
                session_ref=session_ref,
            )
            # After REPL exits, proactively close any attached ChatSession to release resources
            try:
                sess = getattr(agent_obj, "session", None)
                if sess is not None:
                    asyncio.run(sess.aclose())
                    agent_obj.session = None
            except Exception:
                pass
        finally:
            try:
                reporter.stop()
            except Exception:
                pass
        sys.exit(0)

    # Non-interactive
    if initial_prompt:

        async def _run_once_noninteractive() -> None:
            from gptsh.core.sessions import (
                new_session_doc as _new_session_doc,
                resolve_small_model as _resolve_small_model,
                save_session as _save_session,
            )
            from gptsh.core.config_api import get_sessions_enabled

            mcp_manager = None if no_tools_effective else MCPManager(config)

            # Decide if sessions are enabled
            sessions_enabled = get_sessions_enabled(config, no_sessions_cli=no_sessions)

            if not sessions_enabled:
                # Plain non-persistent run
                req = RunRequest(
                    agent=agent_obj,
                    prompt=initial_prompt,
                    config=config,
                    stream=stream,
                    output_format=output_effective,
                    no_tools=no_tools_effective,
                    logger=logger,
                    exit_on_interrupt=True,
                    result_sink=None,
                    messages_sink=None,
                    mcp_manager=mcp_manager,
                    progress_reporter=reporter,
                    session=None,
                )
                await run_turn_with_request(req)
                return

            # Prepare session doc (preloaded or new)
            preloaded_doc: Optional[Dict[str, Any]] = None
            if session_ref:
                sid = _resolve_session_ref(str(session_ref))
                preloaded_doc = _load_session(sid)

            doc = preloaded_doc
            if doc is None:
                chosen_model = (getattr(agent_obj.llm, "_base", {}) or {}).get("model")
                agent_info = {
                    "name": getattr(agent_obj, "name", None),
                    "model": chosen_model,
                    "model_small": _resolve_small_model(agent_conf or {}, provider_conf or {})
                    or chosen_model,
                    "prompt_system": (((agent_conf or {}).get("prompt") or {}).get("system")),
                    "params": {
                        k: v
                        for k, v in (agent_conf or {}).items()
                        if k in {"temperature", "reasoning_effort"} and v is not None
                    },
                }
                provider_info = {
                    "name": (agent_conf or {}).get("provider")
                    or (config.get("default_provider") or None)
                }
                doc = _new_session_doc(
                    agent_info=agent_info,
                    provider_info=provider_info,
                    output=output_effective,
                    mcp_allowed_servers=(config.get("mcp", {}) or {}).get("allowed_servers"),
                )
                # Save once to assign id/filename
                _save_session(doc)

            # Run once with persistence
            req = RunRequest(
                agent=agent_obj,
                prompt=initial_prompt,
                config=config,
                stream=stream,
                output_format=output_effective,
                no_tools=no_tools_effective,
                logger=logger,
                exit_on_interrupt=True,
                result_sink=None,
                messages_sink=None,
                mcp_manager=mcp_manager,
                progress_reporter=reporter,
                session=None,
                session_doc=doc,
                small_model=(doc.get("agent") or {}).get("model_small")
                or (doc.get("agent") or {}).get("model"),
            )
            from gptsh.core.runner import run_turn_with_persistence

            await run_turn_with_persistence(req)

        asyncio.run(_run_once_noninteractive())

        try:
            reporter.stop()
        except Exception:
            pass
    else:
        raise click.UsageError(
            "A prompt is required. Provide via CLI argument, stdin, or agent config's 'user' prompt."
        )


async def run_llm(
    *,
    prompt: str,
    stream: bool,
    output_format: str,
    no_tools: bool,
    config: Dict[str, Any],
    logger: Any,
    exit_on_interrupt: bool = True,
    preinitialized_mcp: bool = False,
    result_sink: Optional[List[str]] = None,
    messages_sink: Optional[List[Dict[str, Any]]] = None,
    agent_obj: Optional[Any] = None,
    mcp_manager: Optional[MCPManager] = None,
    progress_reporter: Optional[Any] = None,
) -> None:
    # Reuse or attach a persistent ChatSession for REPL calls via Agent.session
    session_obj = None
    if agent_obj is not None and exit_on_interrupt is False:
        session_obj = getattr(agent_obj, "session", None)
        if session_obj is None:
            from gptsh.core.session import ChatSession as _ChatSession

            try:
                session_obj = _ChatSession.from_agent(
                    agent_obj,
                    progress=progress_reporter,
                    config=config,
                    mcp=(None if no_tools else (mcp_manager or MCPManager(config))),
                )
                await session_obj.start()
                agent_obj.session = session_obj
            except Exception:
                session_obj = None

    req = RunRequest(
        agent=agent_obj,
        prompt=prompt,
        config=config,
        stream=stream,
        output_format=output_format,
        no_tools=no_tools,
        logger=logger,
        exit_on_interrupt=exit_on_interrupt,
        result_sink=result_sink,
        messages_sink=messages_sink,
        mcp_manager=mcp_manager,
        progress_reporter=progress_reporter,
        session=session_obj,
    )
    await run_turn_with_request(req)


if __name__ == "__main__":
    # Invoke CLI with default standalone mode but no exception catching, so SIGINT propagates
    main(standalone_mode=True, catch_exceptions=False)
