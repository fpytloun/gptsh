import asyncio
import sys
import json
import time
import warnings
# Suppress known LiteLLM RuntimeWarning about un-awaited coroutine on loop close.
warnings.filterwarnings(
    "ignore",
    message=r".*coroutine 'close_litellm_async_clients' was never awaited.*",
    category=RuntimeWarning,
)
import click
from click.core import ParameterSource
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.mcp import list_tools, get_auto_approved_tools, ensure_sessions_started_async
from gptsh.llm.session import (
    prepare_completion_params,
    stream_completion,
    complete_with_tools,
    complete_simple,
)
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from rich.markdown import Markdown

# Ensure LiteLLM async HTTPX clients are closed cleanly on loop shutdown
try:
    from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients  # type: ignore
except Exception:
    close_litellm_async_clients = None  # type: ignore

from typing import Any, Dict, Optional, List

DEFAULT_AGENTS = {
    "default": {}
}

# --- CLI Entrypoint ---

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--provider", default=None, help="Override LiteLLM provider from config")
@click.option("--model", default=None, help="Override LLM model")
@click.option("--agent", default=None, help="Named agent preset from config")
@click.option("--config", "config_path", default=None, help="Specify alternate config path")
@click.option("--stream/--no-stream", default=True)
@click.option("--progress/--no-progress", default=True)
@click.option("--debug", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging (INFO)")
@click.option("--mcp-servers", "mcp_servers", default=None, help="Override path to MCP servers file")
@click.option("--list-tools", "list_tools_flag", is_flag=True, default=False)
@click.option("--list-providers", "list_providers_flag", is_flag=True, default=False, help="List configured providers")
@click.option("--list-agents", "list_agents_flag", is_flag=True, default=False, help="List configured agents and their tools")
@click.option("--output", "-o", type=click.Choice(["text", "markdown"]), default="markdown", help="Output format")
@click.option("--no-tools", is_flag=True, default=False, help="Disable MCP tools (discovery and execution)")
@click.option("--tools", "tools_filter", default=None, help="Comma/space-separated MCP server labels to allow (others skipped)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Run in interactive REPL mode")
@click.argument("prompt", required=False)
def main(provider, model, agent, config_path, stream, progress, debug, verbose, mcp_servers, list_tools_flag, list_providers_flag, list_agents_flag, output, no_tools, tools_filter, interactive, prompt):
    """gptsh: Modular shell/LLM agent client."""
    # Load config
    # Load configuration: use custom path or defaults
    if config_path:
        config = load_config([config_path])
    else:
        config = load_config()

    if mcp_servers:
        # Allow comma or whitespace-separated list of paths
        parts = [p for raw in mcp_servers.split(",") for p in raw.split() if p]
        config.setdefault("mcp", {})["servers_files"] = parts if parts else []
    if tools_filter:
        # Allow comma or whitespace-separated list of server labels
        labels = [p for raw in tools_filter.split(",") for p in raw.split() if p]
        config.setdefault("mcp", {})["allowed_servers"] = labels if labels else []
    # Logging: default WARNING, -v/--verbose -> INFO, --debug -> DEBUG
    log_level = "DEBUG" if debug else ("INFO" if verbose else "WARNING")
    log_fmt = config.get("logging", {}).get("format", "text")
    logger = setup_logging(log_level, log_fmt)

    # Handle immediate listing flags
    if list_tools_flag:
        if no_tools:
            click.echo("MCP tools disabled by --no-tools")
            sys.exit(0)
        tools_map = list_tools(config)
        # Determine selected agent config for agent-level autoApprove
        agents_conf = config.get("agents") or {}
        selected_agent_conf = None
        if isinstance(agents_conf, dict):
            # Use CLI agent if provided; otherwise fall back to config default_agent or 'default'
            if agent:
                selected_agent_conf = agents_conf.get(agent)
            if selected_agent_conf is None:
                default_agent_name = config.get("default_agent") or "default"
                selected_agent_conf = agents_conf.get(default_agent_name) or (DEFAULT_AGENTS.get(default_agent_name) if isinstance(DEFAULT_AGENTS, dict) else None)
        if selected_agent_conf is None:
            # Fallback to built-in default agent mapping using CLI-provided name as last resort
            selected_agent_conf = (DEFAULT_AGENTS.get(agent) if isinstance(DEFAULT_AGENTS, dict) else None)
        approved_map = get_auto_approved_tools(config, agent_conf=selected_agent_conf)
        total_servers = len(tools_map)
        click.echo(f"Discovered tools ({total_servers} server{'s' if total_servers != 1 else ''}):")
        for server, tools in tools_map.items():
            approved_set = set(approved_map.get(server, []))
            global_tools = set(approved_map.get("*", []))
            click.echo(f"{server} ({len(tools)}):")
            if tools:
                for tool in tools:
                    # Badge if tool is explicitly approved, globally approved by name, or server wildcard
                    badge = " 󰁪" if ("*" in approved_set or tool in approved_set or tool in global_tools) else ""
                    click.echo(f"  - {tool}{badge}")
            else:
                click.echo("  (no tools found or discovery failed)")
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

        providers_conf = config.get("providers", {}) or {}
        default_provider_name = config.get("default_provider") or (next(iter(providers_conf)) if providers_conf else None)

        # Discover tools once (unless tools disabled)
        tools_map = {}
        if not no_tools:
            try:
                tools_map = list_tools(config)
            except Exception:
                tools_map = {}

        click.echo("Configured agents:")
        for agent_name, aconf in agents_conf.items():
            if not isinstance(aconf, dict):
                aconf = {}
            # Determine effective provider and model for this agent
            agent_provider = aconf.get("provider") or default_provider_name
            chosen_model = aconf.get("model") or ((providers_conf.get(agent_provider) or {}).get("model")) or "?"
            click.echo(f"- {agent_name}")
            click.echo(f"  provider: {agent_provider or '?'}")
            click.echo(f"  model: {chosen_model}")

            # Determine allowed servers per agent (None = all)
            tools_field = aconf.get("tools")
            allowed_servers: Optional[List[str]] = None
            if isinstance(tools_field, list):
                allowed_servers = [str(x) for x in tools_field if x is not None]
                if len(allowed_servers) == 0:
                    click.echo(f"  tools: (disabled)")
                    continue

            # Compute auto-approved map for this agent
            try:
                approved_map = get_auto_approved_tools(config, agent_conf=aconf)
            except Exception:
                approved_map = {}

            if no_tools:
                click.echo("  tools: (disabled by --no-tools)")
                continue

            # Collect servers to display
            server_names = list(tools_map.keys())
            if allowed_servers is not None:
                server_names = [s for s in server_names if s in allowed_servers]

            if not server_names:
                click.echo("  tools: (none discovered)")
                continue

            click.echo("  tools:")
            for server in server_names:
                names = tools_map.get(server, []) or []
                click.echo(f"    {server} ({len(names)}):")
                if names:
                    approved_set = set(approved_map.get(server, []) or [])
                    global_set = set(approved_map.get("*", []) or [])
                    for t in names:
                        badge = " 󰁪" if ("*" in approved_set or t in approved_set or t in global_set) else ""
                        click.echo(f"      - {t}{badge}")
                else:
                    click.echo("      (no tools found or discovery failed)")
        sys.exit(0)

    # Ensure a default agent always exists by merging built-ins into config
    existing_agents = dict(config.get("agents") or {})
    config["agents"] = {**DEFAULT_AGENTS, **existing_agents}

    # Resolve provider and agent defaults
    providers_conf = config.get("providers", {})
    if not providers_conf:
        raise click.ClickException("No providers defined in config.")

    agents_conf = config.get("agents", DEFAULT_AGENTS)
    # CLI should take precedence over config default
    agent = agent or config.get("default_agent") or "default"
    if agent not in agents_conf:
        raise click.BadParameter(f"Unknown agent '{agent}'", param_hint="--agent")
    agent_conf = agents_conf[agent]

    # Determine effective provider: CLI --provider > agent.provider > config default_provider > first configured
    selected_provider = provider or (agent_conf.get("provider") if isinstance(agent_conf, dict) else None) or config.get("default_provider") or next(iter(providers_conf))
    if selected_provider not in providers_conf:
        raise click.BadParameter(f"Unknown provider '{selected_provider}'", param_hint="--provider")
    provider_conf = providers_conf[selected_provider]

    # Interactive REPL mode
    if interactive:
        # Allow stdin to carry an initial prompt; require a TTY on stdout for REPL display
        if not sys.stdout.isatty():
            raise click.ClickException("Interactive mode requires a TTY on stdout.")
        # Build an initial prompt for the first REPL turn from positional arg and/or stdin
        initial_prompt = None
        try:
            stdin_input = None if sys.stdin.isatty() else read_stdin()
        except Exception:
            stdin_input = None
        if prompt and stdin_input:
            initial_prompt = f"{prompt}\n\n---\nInput:\n{stdin_input}"
        else:
            initial_prompt = prompt or stdin_input
        # Agent-level overrides for tools if CLI flags not provided
        no_tools_effective = no_tools or bool(agent_conf.get("no_tools"))
        if not tools_filter:
            agent_tools = agent_conf.get("tools")
            if isinstance(agent_tools, list):
                if len(agent_tools) == 0:
                    # Treat empty tools list as disabling tools entirely
                    no_tools_effective = True
                else:
                    labels = [str(x) for x in agent_tools if x]
                    config.setdefault("mcp", {})["allowed_servers"] = labels
        # Determine effective output format: CLI --output takes precedence over agent config.
        output_effective = output
        try:
            src = click.get_current_context().get_parameter_source("output")
        except Exception:
            src = None
        if src != ParameterSource.COMMANDLINE:
            agent_output = agent_conf.get("output") if isinstance(agent_conf, dict) else None
            if agent_output in ("text", "markdown"):
                output_effective = agent_output
        repl_loop(
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=model,
            stream=stream,
            progress=progress,
            output_format=output_effective,
            no_tools=no_tools_effective,
            config=config,
            logger=logger,
            agent_name=agent,
            initial_prompt=initial_prompt,
        )
        sys.exit(0)
    # Handle prompt or stdin
    stdin_input = None
    if not sys.stdin.isatty():
        stdin_input = read_stdin()
    # Try to get prompt from agent config
    agent_prompt = agent_conf.get("prompt", {}).get("user") if agent_conf else None
    # Combine prompt and piped stdin if both are provided
    if prompt and stdin_input:
        prompt_given = f"{prompt}\n\n---\nInput:\n{stdin_input}"
    else:
        prompt_given = prompt or stdin_input or agent_prompt
    if prompt_given:
        # Agent-level overrides for tools if CLI flags not provided
        no_tools_effective = no_tools or bool(agent_conf.get("no_tools"))
        if not tools_filter:
            agent_tools = agent_conf.get("tools")
            if isinstance(agent_tools, list):
                if len(agent_tools) == 0:
                    # Treat empty tools list as disabling tools entirely
                    no_tools_effective = True
                else:
                    labels = [str(x) for x in agent_tools if x]
                    config.setdefault("mcp", {})["allowed_servers"] = labels

        # Determine effective output format: CLI --output takes precedence over agent config.
        output_effective = output
        try:
            src = click.get_current_context().get_parameter_source("output")
        except Exception:
            src = None
        if src != ParameterSource.COMMANDLINE:
            agent_output = agent_conf.get("output") if isinstance(agent_conf, dict) else None
            if agent_output in ("text", "markdown"):
                output_effective = agent_output

        asyncio.run(run_llm(
            prompt=prompt_given,
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=model,
            stream=stream,
            progress=progress,
            output_format=output_effective,
            no_tools=no_tools_effective,
            config=config,
            logger=logger,
        ))
    else:
        raise click.UsageError("A prompt is required. Provide via CLI argument, stdin, or agent config's 'user' prompt.")


async def run_llm(
      prompt: str,
      provider_conf: Dict[str, Any],
      agent_conf: Optional[Dict[str, Any]],
      cli_model_override: Optional[str],
      stream: bool,
      progress: bool,
      output_format: str,
      no_tools: bool,
      config: Dict[str, Any],
      logger: Any,
      exit_on_interrupt: bool = True,
      preinitialized_mcp: bool = False,
      history_messages: Optional[List[Dict[str, Any]]] = None,
      result_sink: Optional[List[str]] = None,
  ) -> None:
    """Execute an LLM call using LiteLLM with optional streaming.
    Rendering and progress UI remain in CLI; core LLM/session logic lives in gptsh.llm.session.
    """
    # Setup rich progress (spinner) if enabled
    progress_obj: Optional[Progress] = None
    progress_running: bool = False
    console = Console()
    if progress and sys.stderr.isatty():
        progress_console = Console(file=sys.stderr)
        progress_obj = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            transient=True,
            console=progress_console,
        )
        progress_obj.start()
        progress_running = True

    # Prepare LiteLLM params (build messages/model/tools) via session
    init_task_id = None
    try:
        # Ensure MCP servers are started once and kept for the duration of the run
        if not no_tools and not preinitialized_mcp:
            if progress_obj is not None:
                init_label = "Initializing MCP tools"
                init_task_id = progress_obj.add_task(init_label, total=None)
            try:
                await ensure_sessions_started_async(config)
            except Exception:
                # Do not fail request if MCP init fails; discovery/execution will handle per-server errors
                pass
        params, has_tools, chosen_model = await prepare_completion_params(
            prompt=prompt,
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=cli_model_override,
            config=config,
            no_tools=no_tools,
            history_messages=history_messages,
        )
    finally:
        if init_task_id is not None and progress_obj is not None:
            try:
                progress_obj.remove_task(init_task_id)
            except Exception:
                pass

    # If tools are present, force non-stream path
    if has_tools:
        stream = False

    waiting_task_id: Optional[int] = None
    try:
        if stream:
            wait_label = f"Waiting for {chosen_model.rsplit('/', 1)[-1]}"
            if progress_obj is not None:
                waiting_task_id = progress_obj.add_task(wait_label, total=None)
            md_buffer = "" if output_format == "markdown" else ""
            first_output_done = False
            full_output = ""
            async for text in stream_completion(params):
                if not text:
                    continue
                # Ensure spinner is ended before any output
                if not first_output_done:
                    if waiting_task_id is not None and progress_obj is not None:
                        try:
                            progress_obj.remove_task(waiting_task_id)
                        except Exception:
                            pass
                        waiting_task_id = None
                    if progress_obj is not None and progress_running:
                        try:
                            progress_obj.stop()
                        except Exception:
                            pass
                        progress_running = False
                    # Clear any potential leftover line from progress UI to avoid a leading blank line
                    if sys.stderr.isatty():
                        try:
                            sys.stderr.write("\x1b[1A\x1b[2K")
                            sys.stderr.flush()
                        except Exception:
                            pass
                    first_output_done = True
                if output_format == "markdown":
                    md_buffer += text
                    # Stream out complete lines as Markdown
                    while "\n" in md_buffer:
                        line, md_buffer = md_buffer.split("\n", 1)
                        console.print(Markdown(line))
                else:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                full_output += text
            # After stream ends
            if output_format == "markdown":
                if md_buffer:
                    console.print(Markdown(md_buffer))
            else:
                click.echo()  # newline
            # Capture full output for history if requested
            if result_sink is not None:
                try:
                    result_sink.append(full_output)
                except Exception:
                    pass
        else:
            wait_label = f"Waiting for {chosen_model.rsplit('/', 1)[-1]}"
            if progress_obj is not None:
                waiting_task_id = progress_obj.add_task(wait_label, total=None)
            # Non-streaming paths
            if has_tools:
                approved_map = get_auto_approved_tools(config, agent_conf=agent_conf)

                def pause_ui():
                    nonlocal waiting_task_id, progress_obj, progress_running
                    # Remove current waiting task
                    if waiting_task_id is not None and progress_obj is not None:
                        try:
                            progress_obj.remove_task(waiting_task_id)
                        except Exception:
                            pass
                        waiting_task_id = None
                    # Stop spinner so prompt won't be overwritten
                    if progress_obj is not None and progress_running:
                        try:
                            progress_obj.stop()
                        except Exception:
                            pass
                        progress_running = False

                def resume_ui():
                    nonlocal waiting_task_id, progress_obj, progress_running, chosen_model
                    # Restart spinner if needed
                    if progress_obj is not None and not progress_running:
                        try:
                            progress_obj.start()
                        except Exception:
                            pass
                        progress_running = True
                    # Recreate waiting task
                    if progress_obj is not None:
                        try:
                            waiting_task_id = progress_obj.add_task(wait_label, total=None)
                        except Exception:
                            waiting_task_id = None

                def set_status(text: Optional[str]):
                    nonlocal waiting_task_id, progress_obj, progress_running
                    # If no progress UI or no text, nothing to do
                    if progress_obj is None or not text:
                        return
                    # Ensure a waiting task exists; if not, create one with the given description
                    if waiting_task_id is None:
                        try:
                            waiting_task_id = progress_obj.add_task(text, total=None)
                        except Exception:
                            waiting_task_id = None
                            return
                        # Make sure the spinner is running
                        if not progress_running:
                            try:
                                progress_obj.start()
                            except Exception:
                                pass
                            else:
                                progress_running = True
                        return
                    # Update existing task description
                    try:
                        progress_obj.update(waiting_task_id, description=text, refresh=True)
                        try:
                            progress_obj.refresh()
                        except Exception:
                            pass
                    except Exception:
                        pass

                content = await complete_with_tools(
                    params,
                    config,
                    approved_map,
                    pause_ui=pause_ui,
                    resume_ui=resume_ui,
                    set_status=set_status,
                    wait_label=wait_label,
                )
            else:
                content = await complete_simple(params)
            # Capture output for history if requested
            if result_sink is not None:
                try:
                    result_sink.append(content or "")
                except Exception:
                    pass
            # Stop waiting indicator before printing final output
            if waiting_task_id is not None and progress_obj is not None:
                try:
                    progress_obj.remove_task(waiting_task_id)
                except Exception:
                    pass
                waiting_task_id = None
            if progress_obj is not None and progress_running:
                try:
                    progress_obj.stop()
                except Exception:
                    pass
                progress_running = False
            # Clear any potential leftover line from progress UI to avoid a leading blank line
            if sys.stderr.isatty():
                try:
                    sys.stderr.write("\x1b[1A\x1b[2K")
                    sys.stderr.flush()
                except Exception:
                    pass
            if output_format == "markdown":
                console.print(Markdown(content or ""))
            else:
                click.echo(content or "")
    except KeyboardInterrupt:
        if exit_on_interrupt:
            click.echo("", err=True)
            sys.exit(130)
        else:
            raise
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        sys.exit(1)
    finally:
        # Ensure waiting indicator is cleared and progress stopped
        if waiting_task_id is not None and progress_obj is not None:
            try:
                progress_obj.remove_task(waiting_task_id)
            except Exception:
                pass
            waiting_task_id = None
        if progress_obj is not None and progress_running:
            try:
                progress_obj.stop()
            except Exception:
                pass
            progress_running = False

def repl_loop(
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]],
    cli_model_override: Optional[str],
    stream: bool,
    progress: bool,
    output_format: str,
    no_tools: bool,
    config: Dict[str, Any],
    logger: Any,
    agent_name: Optional[str],
    initial_prompt: Optional[str],
) -> None:
    """
    Simple interactive REPL using GNU readline when available.
    - Up/Down for history navigation
    - Ctrl+R for reverse history search (readline-provided)
    - Ctrl+C cancels in-flight request; press twice quickly to exit
    - Ctrl+D (EOF) exits
    """
    # Create a persistent event loop for the entire REPL session so MCP sessions persist
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    mgr = None
    # Initialize MCP sessions before entering REPL so first command is fast
    if not no_tools:
        progress_obj = None
        init_task_id = None
        progress_running = False
        try:
            if sys.stderr.isatty():
                progress_console = Console(file=sys.stderr)
                progress_obj = Progress(
                    SpinnerColumn(),
                    TextColumn("{task.description}"),
                    transient=True,
                    console=progress_console,
                )
                progress_obj.start()
                progress_running = True
                init_task_id = progress_obj.add_task("Initializing MCP tools", total=None)
            mgr = loop.run_until_complete(ensure_sessions_started_async(config))
        except Exception:
            pass
        finally:
            if init_task_id is not None and progress_obj is not None:
                try:
                    progress_obj.remove_task(init_task_id)
                except Exception:
                    pass
                init_task_id = None
            if progress_obj is not None and progress_running:
                try:
                    progress_obj.stop()
                except Exception:
                    pass
                progress_running = False
            # Clear any potential leftover line from progress UI to avoid a leading blank line
            if sys.stderr.isatty():
                try:
                    sys.stderr.write("\x1b[1A\x1b[2K")
                    sys.stderr.flush()
                except Exception:
                    pass
    # If stdin is not a TTY (e.g., initial prompt was piped), reattach stdin to /dev/tty for interactive input
    tty_in = None
    if not sys.stdin.isatty():
        try:
            tty_in = open("/dev/tty", "r")
            sys.stdin = tty_in
        except Exception:
            tty_in = None
    # Best-effort enable readline features
    _readline = None
    try:
        import readline as _readline  # type: ignore
        try:
            _readline.parse_and_bind("tab: complete")
            # Default emacs bindings include Ctrl+R reverse-search-history
        except Exception:
            pass
    except Exception:
        _readline = None

    # Build a nice, colored prompt: "<agent>|<model>> "
    chosen_model = (
        cli_model_override
        or (agent_conf or {}).get("model")
        or provider_conf.get("model")
        or "?"
    )
    model_label = str(chosen_model).rsplit("/", 1)[-1]
    agent_label = agent_name or "default"
    agent_col = click.style(agent_label, fg="cyan", bold=True)
    model_col = click.style(model_label, fg="magenta")
    prompt_str = f"{agent_col}|{model_col}> "
    history_messages: List[Dict[str, Any]] = []
    last_interrupt = 0.0

    # If an initial prompt was provided (via stdin or positional arg), run it once before REPL input
    if initial_prompt and str(initial_prompt).strip():
        try:
            result_holder: List[str] = []
            user_msg: Dict[str, Any] = {"role": "user", "content": initial_prompt}
            loop.run_until_complete(
                run_llm(
                    prompt=initial_prompt,
                    provider_conf=provider_conf,
                    agent_conf=agent_conf,
                    cli_model_override=cli_model_override,
                    stream=stream,
                    progress=progress,
                    output_format=output_format,
                    no_tools=no_tools,
                    config=config,
                    logger=logger,
                    exit_on_interrupt=False,
                    preinitialized_mcp=True,
                    history_messages=history_messages,
                    result_sink=result_holder,
                )
            )
            assistant_content = result_holder[0] if result_holder else ""
            history_messages.extend([user_msg, {"role": "assistant", "content": assistant_content}])
        except KeyboardInterrupt:
            last_interrupt = time.monotonic()
            click.echo("Cancelled.", err=True)
    while True:
        try:
            line = input(prompt_str)
        except KeyboardInterrupt:
            now = time.monotonic()
            # Double Ctrl-C within 1.5s exits
            if now - last_interrupt <= 1.5:
                click.echo("", err=True)
                break
            last_interrupt = now
            click.echo("(^C) Press Ctrl-C again to exit", err=True)
            continue
        except EOFError:
            click.echo("", err=True)
            break

        if not line.strip():
            continue

        # Add to session history if readline is available
        if _readline is not None:
            try:
                _readline.add_history(line)
            except Exception:
                pass

        try:
            # Prepare to capture assistant reply to maintain conversation history
            result_holder: List[str] = []
            user_msg: Dict[str, Any] = {"role": "user", "content": line}
            loop.run_until_complete(
                run_llm(
                    prompt=line,
                    provider_conf=provider_conf,
                    agent_conf=agent_conf,
                    cli_model_override=cli_model_override,
                    stream=stream,
                    progress=progress,
                    output_format=output_format,
                    no_tools=no_tools,
                    config=config,
                    logger=logger,
                    exit_on_interrupt=False,
                    preinitialized_mcp=True,
                    history_messages=history_messages,
                    result_sink=result_holder,
                )
            )
            # Update history with user and assistant messages
            assistant_content = result_holder[0] if result_holder else ""
            history_messages.extend([user_msg, {"role": "assistant", "content": assistant_content}])
        except KeyboardInterrupt:
            last_interrupt = time.monotonic()
            click.echo("Cancelled.", err=True)
            continue

    # Clean up MCP sessions and close the persistent event loop
    try:
        if not no_tools and mgr is not None:
            loop.run_until_complete(mgr.stop())
    except Exception:
        pass
    # Cleanup LiteLLM async clients to avoid un-awaited coroutine warnings
    try:
        if close_litellm_async_clients is not None:
            loop.run_until_complete(close_litellm_async_clients())
    except Exception:
        pass
    # Close reattached TTY input if opened
    try:
        if 'tty_in' in locals() and tty_in is not None:
            tty_in.close()
    except Exception:
        pass
    try:
        # Suppress known RuntimeWarning from litellm's async_client_cleanup when closing loop
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=RuntimeWarning,
                module=r"litellm\.llms\.custom_httpx\.async_client_cleanup",
            )
            loop.close()
    except Exception:
        pass

if __name__ == "__main__":
    main()
