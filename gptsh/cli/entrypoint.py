import asyncio
import os
import sys
import warnings

import click

from gptsh.cli.utils import (
    is_tty as _is_tty,
    print_agents_listing as _print_agents_listing,
    print_tools_listing as _print_tools_listing,
    resolve_agent_and_settings as _resolve_agent_and_settings,
)
from gptsh.config.loader import load_config
from gptsh.core.config_resolver import build_agent
from gptsh.core.logging import setup_logging
from gptsh.core.repl import run_agent_repl
from gptsh.core.runner import RunRequest, run_turn_with_request
from gptsh.mcp.manager import MCPManager
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
@click.option("--output", "-o", type=click.Choice(["text", "markdown", "default"]), default="default", help="Output format")
@click.option("--no-tools", is_flag=True, default=False, help="Disable MCP tools (discovery and execution)")
@click.option("--tools", "tools_filter", default=None, help="Comma/space-separated MCP server labels to allow (others skipped)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Run in interactive REPL mode")
@click.option("--assume-tty", is_flag=True, default=False, help="Assume TTY (for tests/CI)")
@click.argument("prompt", required=False)
def main(provider, model, agent, config_path, stream, progress, debug, verbose, mcp_servers, list_tools_flag, list_providers_flag, list_agents_flag, output, no_tools, tools_filter, interactive, assume_tty, prompt):
    """gptsh: Modular shell/LLM agent client."""
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
            agent_conf=(config.get("agents") or {}).get(agent or (config.get("default_agent") or "default")),
        )
        tools_map = {srv: [h.name for h in (agent_obj.tools or {}).get(srv, [])] for srv in (agent_obj.tools or {})}
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

    # Resolve agent
    agent_obj, agent_conf, provider_conf, output_effective, no_tools_effective, _ = asyncio.run(
        _resolve_agent_and_settings(
            config=config,
            agent_name=agent,
            provider_name=provider,
            model_override=model,
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
            raise click.ClickException("Failed to decode input. Please ensure UTF-8 encoding.") from e

        # We consumed something from stdin and have tty on stderr so session seems interactive, open /dev/tty for interactive inputs (tool approvals)
        if stdin_input and _is_tty(stream="stderr"):
            try:
                sys.stdin = open("/dev/tty", "r", encoding="utf-8", errors="replace")
            except OSError:
                # We cannot re-open stdin so assume session is not interactive
                pass

    # Construct prompt
    prompt = prompt or agent_conf.get("prompt", {}).get("user")
    initial_prompt = f"{prompt}\n\n---\nInput:\n{stdin_input}" if (prompt and stdin_input) else (prompt or stdin_input)

    # Interactive REPL mode
    if interactive:
        if not (_is_tty(assume_tty=assume_tty, stream="stdout") and _is_tty(assume_tty=assume_tty, stream="stdin")):
            raise click.ClickException("Interactive mode requires a TTY.")

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

    # Non-interactive
    if initial_prompt:
        async def _run_llm_once(*args, **kwargs):
            await run_llm(*args, **kwargs)

        asyncio.run(_run_llm_once(
            prompt=initial_prompt,
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


async def run_llm(*, prompt: str, provider_conf: Dict[str, Any], agent_conf: Optional[Dict[str, Any]], cli_model_override: Optional[str], stream: bool, progress: bool, output_format: str, no_tools: bool, config: Dict[str, Any], logger: Any, exit_on_interrupt: bool = True, preinitialized_mcp: bool = False, history_messages: Optional[List[Dict[str, Any]]] = None, result_sink: Optional[List[str]] = None, messages_sink: Optional[List[Dict[str, Any]]] = None, agent_obj: Optional[Any] = None, mcp_manager: Optional[MCPManager] = None) -> None:
    req = RunRequest(
        agent=agent_obj,
        prompt=prompt,
        config=config,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        stream=stream,
        progress=progress,
        output_format=output_format,
        no_tools=no_tools,
        logger=logger,
        exit_on_interrupt=exit_on_interrupt,
        history_messages=history_messages,
        result_sink=result_sink,
        messages_sink=messages_sink,
        mcp_manager=mcp_manager,
    )
    await run_turn_with_request(req)


if __name__ == "__main__":
    main()
