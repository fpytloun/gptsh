import asyncio
import sys
import json
import click
from click.core import ParameterSource
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.mcp import list_tools, get_auto_approved_tools
from gptsh.llm.session import (
    prepare_completion_params,
    stream_completion,
    complete_with_tools,
    complete_simple,
)
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from rich.markdown import Markdown

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
@click.argument("prompt", required=False)
def main(provider, model, agent, config_path, stream, progress, debug, verbose, mcp_servers, list_tools_flag, list_providers_flag, list_agents_flag, output, no_tools, tools_filter, prompt):
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
        if progress_obj is not None:
            init_label = "Initializing MCP tools" if not no_tools else "Preparing request"
            init_task_id = progress_obj.add_task(init_label, total=None)
        params, has_tools, chosen_model = await prepare_completion_params(
            prompt=prompt,
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=cli_model_override,
            config=config,
            no_tools=no_tools,
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
            # After stream ends
            if output_format == "markdown":
                if md_buffer:
                    console.print(Markdown(md_buffer))
            else:
                click.echo()  # newline
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
                    nonlocal waiting_task_id, progress_obj
                    if waiting_task_id is not None and progress_obj is not None and text:
                        try:
                            progress_obj.update(waiting_task_id, description=text)
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
        click.echo("", err=True)
        sys.exit(130)
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

if __name__ == "__main__":
    main()
