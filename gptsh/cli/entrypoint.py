import asyncio
import sys
import warnings

import click
from rich.console import Console
from rich.markdown import Markdown

from gptsh.config.loader import load_config
from gptsh.core.api import run_prompt_with_agent
from gptsh.core.config_resolver import build_agent
from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.core.logging import setup_logging
from gptsh.core.progress import RichProgressReporter
from gptsh.core.repl import run_agent_repl
from gptsh.core.session import ChatSession
from gptsh.core.stdin_handler import read_stdin
from gptsh.mcp.api import get_auto_approved_tools, list_tools

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

DEFAULT_AGENTS = {
    "default": {}
}

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
    # Pre-parse CLI tools filter into list to later apply via config_api
    tools_filter_labels = None
    if tools_filter:
        tools_filter_labels = [p for raw in tools_filter.split(",") for p in raw.split() if p]
    # Logging: default WARNING, -v/--verbose -> INFO, --debug -> DEBUG
    log_level = "DEBUG" if debug else ("INFO" if verbose else "WARNING")
    log_fmt = config.get("logging", {}).get("format", "text")
    logger = setup_logging(log_level, log_fmt)

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
        except Exception:
            # Fallback to direct MCP listing if agent resolution fails (e.g., no providers in stub tests)
            tools = list_tools(config)
            click.echo(f"Discovered tools ({len(tools)} server{'s' if len(tools) != 1 else ''}):")
            for server, names in tools.items():
                click.echo(f"{server} ({len(names)}):")
                if names:
                    for n in names:
                        click.echo(f"  - {n}")
                else:
                    click.echo("  (no tools found or discovery failed)")
            sys.exit(0)
        if agent_obj is None:
            click.echo("Failed to resolve agent/tools")
            sys.exit(1)
        approved_map = get_auto_approved_tools(config, agent_conf=(config.get("agents") or {}).get(agent or (config.get("default_agent") or "default")))
        tools_map = agent_obj.tools or {}
        total_servers = len(tools_map)
        click.echo(f"Discovered tools ({total_servers} server{'s' if total_servers != 1 else ''}):")
        for server, handles in tools_map.items():
            approved_set = set(approved_map.get(server, []) or [])
            global_tools = set(approved_map.get("*", []) or [])
            click.echo(f"{server} ({len(handles)}):")
            if handles:
                for h in handles:
                    badge = " 󰁪" if ("*" in approved_set or h.name in approved_set or h.name in global_tools) else ""
                    click.echo(f"  - {h.name}{badge}")
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
                    click.echo("  tools: (disabled)")
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

    # Ensure a default agent always exists by merging built-ins into config, then map via config_api
    existing_agents = dict(config.get("agents") or {})
    config["agents"] = {**DEFAULT_AGENTS, **existing_agents}
    from gptsh.core.config_api import (
        compute_tools_policy,
        effective_output,
        select_agent_provider_dicts,
    )
    try:
        agent_conf, provider_conf = select_agent_provider_dicts(config, cli_agent=agent, cli_provider=provider)
    except KeyError as e:
        msg = str(e)
        if "agent" in msg:
            raise click.BadParameter(msg, param_hint="--agent") from None
        if "provider" in msg:
            raise click.BadParameter(msg, param_hint="--provider") from None
        raise

    # Interactive REPL mode
    if interactive:
        # Allow stdin to carry an initial prompt; require a TTY on stdout for REPL display
        if not sys.stdout.isatty():
            raise click.ClickException("Interactive mode requires a TTY on stdout.")
        # Initial prompt from arg and/or stdin
        try:
            stdin_input = None if sys.stdin.isatty() else read_stdin()
        except Exception:
            stdin_input = None
        initial_prompt = f"{prompt}\n\n---\nInput:\n{stdin_input}" if (prompt and stdin_input) else (prompt or stdin_input)
        # Agent overrides and building
        labels_cli = tools_filter_labels if tools_filter_labels is not None else None
        no_tools_effective, allowed = compute_tools_policy(agent_conf, labels_cli, no_tools)
        if allowed is not None:
            config.setdefault("mcp", {})["allowed_servers"] = allowed
        output_effective = effective_output(output, agent_conf)
        agent_obj = asyncio.run(
            build_agent(
                config,
                cli_agent=agent,
                cli_provider=provider,
                cli_tools_filter=labels_cli,
                cli_model_override=model,
                cli_no_tools=no_tools_effective,
            )
        )
        # Hand off to agent-only REPL
        run_agent_repl(
            agent=agent_obj,
            config=config,
            output_format=output_effective,
            stream=stream,
            progress=progress,
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
        labels = tools_filter_labels if tools_filter_labels is not None else None
        from gptsh.core.config_api import compute_tools_policy
        no_tools_effective, allowed = compute_tools_policy(agent_conf, labels, no_tools)
        if allowed is not None:
            config.setdefault("mcp", {})["allowed_servers"] = allowed

        # Determine effective output format via config_api
        output_effective = effective_output(output, agent_conf)

        # Build an Agent for this run
        agent_obj = None
        labels_cli = tools_filter_labels
        agent_obj = asyncio.run(
            build_agent(
                config,
                cli_agent=agent,
                cli_provider=provider,
                cli_tools_filter=labels_cli,
                cli_model_override=model,
                cli_no_tools=no_tools_effective,
            )
        )

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
            agent_obj=agent_obj,
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
      agent_obj: Optional[Any] = None,
  ) -> None:
    """Execute an LLM call using LiteLLM with optional streaming.
    Rendering and progress UI remain in CLI; core logic lives in ChatSession.
    """
    # Setup progress reporter if enabled
    pr: Optional[RichProgressReporter] = None
    console = Console()
    if progress and sys.stderr.isatty():
        pr = RichProgressReporter()
        pr.start()

    # Support tools in both streaming and non-streaming paths

    waiting_task_id: Optional[int] = None
    try:
        if stream:
            # Agent-only streaming path
            if agent_obj is None or not hasattr(ChatSession, "from_agent"):
                raise RuntimeError("Agent resolution failed or ChatSession missing from_agent")
            session = ChatSession.from_agent(agent_obj, progress=pr, config=config)
            params, chosen_model = await session.prepare_stream(
                prompt=prompt,
                provider_conf=provider_conf,
                agent_conf=agent_conf,
                cli_model_override=cli_model_override,
                history_messages=history_messages,
            )
            wait_label = f"Waiting for {str(chosen_model).rsplit('/', 1)[-1]}"
            if pr is not None:
                waiting_task_id = pr.add_task(wait_label)
            md_buffer = "" if output_format == "markdown" else ""
            first_output_done = False
            full_output = ""
            async for text in session.stream_with_params(params):
                if not text:
                    continue
                # Ensure spinner is ended before any output
                if not first_output_done:
                    if pr is not None:
                        if waiting_task_id is not None:
                            pr.complete_task(waiting_task_id)
                            waiting_task_id = None
                        # Stop progress to clear spinner before any output
                        try:
                            pr.stop()
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
            # Non-streaming path. Prefer Agent wiring when available; fallback to core API.
            chosen_model = (
                cli_model_override
                or (agent_conf or {}).get("model")
                or provider_conf.get("model")
                or "?"
            )
            wait_label = f"Waiting for {str(chosen_model).rsplit('/', 1)[-1]}"
            if pr is not None:
                waiting_task_id = pr.add_task(wait_label)
            try:
                if agent_obj is None:
                    raise RuntimeError("Agent resolution failed")
                content = await run_prompt_with_agent(
                    agent=agent_obj,
                    prompt=prompt,
                    config=config,
                    provider_conf=provider_conf,
                    agent_conf=agent_conf,
                    cli_model_override=cli_model_override,
                    no_tools=no_tools,
                    history_messages=history_messages,
                    progress_reporter=pr,
                )
            except ToolApprovalDenied as e:
                # Exit code 4: tool approval denied
                if waiting_task_id is not None and pr is not None:
                    pr.complete_task(waiting_task_id)
                    waiting_task_id = None
                click.echo(f"Tool approval denied: {e}", err=True)
                sys.exit(4)
            # Capture output for history if requested
            if result_sink is not None:
                try:
                    result_sink.append(content or "")
                except Exception:
                    pass
            # Stop waiting indicator before printing final output
            if pr is not None:
                if waiting_task_id is not None:
                    pr.complete_task(waiting_task_id)
                    waiting_task_id = None
                try:
                    pr.stop()
                except Exception:
                    pass
            if output_format == "markdown":
                console.print(Markdown(content or ""))
            else:
                click.echo(content or "")
    except asyncio.TimeoutError:
        # Exit code 124: operation timeout
        click.echo("Operation timed out", err=True)
        sys.exit(124)
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
        if waiting_task_id is not None and pr is not None:
            pr.complete_task(waiting_task_id)
            waiting_task_id = None
        if pr is not None:
            try:
                pr.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
