import asyncio
import sys
import click
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.core.mcp import list_tools

from typing import Any, Dict, Optional, List

DEFAULT_AGENTS = {
    "default": {}
}

# --- CLI Entrypoint ---

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--provider", default=None, help="Override LiteLLM provider from config")
@click.option("--model", default=None, help="Override LLM model")
@click.option("--agent", default="default", help="Named agent preset from config")
@click.option("--config", "config_path", default=None, help="Specify alternate config path")
@click.option("--stream/--no-stream", default=True)
@click.option("--progress/--no-progress", default=True)
@click.option("--debug", is_flag=True, default=False)
@click.option("--mcp-servers", "mcp_servers", default=None, help="Override path to MCP servers file")
@click.option("--list-tools", "list_tools_flag", is_flag=True, default=False)
@click.option("--list-providers", "list_providers_flag", is_flag=True, default=False, help="List configured providers")
@click.argument("prompt", required=False)
def main(provider, model, agent, config_path, stream, progress, debug, mcp_servers, list_tools_flag, list_providers_flag, prompt):
    """gptsh: Modular shell/LLM agent client."""
    # Load config
    # Load configuration: use custom path or defaults
    if config_path:
        config = load_config([config_path])
    else:
        config = load_config()

    if mcp_servers:
        config.setdefault("mcp", {})["servers_files"] = [mcp_servers]
    log_level = "DEBUG" if debug else config.get("logging", {}).get("level", "WARNING")
    log_fmt = config.get("logging", {}).get("format", "text")
    logger = setup_logging(log_level, log_fmt)

    # Handle immediate listing flags
    if list_tools_flag:
        tools_map = list_tools(config)
        click.echo("Discovered tools:")
        for server, tools in tools_map.items():
            click.echo(f"{server}:")
            for tool in tools:
                click.echo(f"  - {tool}")
        sys.exit(0)

    if list_providers_flag:
        providers = config.get("providers", {})
        click.echo("Configured providers:")
        for name in providers:
            click.echo(f"  - {name}")
        sys.exit(0)

    # Ensure a default agent always exists by merging built-ins into config
    existing_agents = dict(config.get("agents") or {})
    config["agents"] = {**DEFAULT_AGENTS, **existing_agents}

    # Resolve provider and agent defaults
    providers_conf = config.get("providers", {})
    if not providers_conf:
        raise click.ClickException("No providers defined in config.")
    provider = provider or config.get("default_provider") or next(iter(providers_conf))
    if provider not in providers_conf:
        raise click.BadParameter(f"Unknown provider '{provider}'", param_hint="--provider")
    provider_conf = providers_conf[provider]

    agents_conf = config.get("agents", DEFAULT_AGENTS)
    # CLI should take precedence over config default
    agent = agent or config.get("default_agent") or "default"
    if agent not in agents_conf:
        raise click.BadParameter(f"Unknown agent '{agent}'", param_hint="--agent")
    agent_conf = agents_conf[agent]

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
        asyncio.run(run_llm(
            prompt=prompt_given,
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=model,
            stream=stream,
            progress=progress,
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
      logger: Any,
  ) -> None:
      """Execute an LLM call using LiteLLM with optional streaming."""
      try:
          from litellm import acompletion

          # Build base params from provider configuration, excluding non-LiteLLM keys
          params: Dict[str, Any] = {
              k: v for k, v in dict(provider_conf).items() if k not in {"default_model", "name"}
          }

          # Determine model: CLI override > agent config > provider default > fallback
          chosen_model = (
              cli_model_override
              or (agent_conf or {}).get("model")
              or provider_conf.get("default_model")
              or "gpt-4o"
          )

          # Build messages: system then user
          messages: List[Dict[str, str]] = []
          system_prompt = (agent_conf or {}).get("prompt", {}).get("system")
          if system_prompt:
              messages.append({"role": "system", "content": system_prompt})
          messages.append({"role": "user", "content": prompt})

          params["model"] = chosen_model
          params["messages"] = messages

          logger.info(f"Calling LLM model {chosen_model}")

          if stream:
              if progress:
                  click.echo("Connecting to model...", err=True)
              stream_iter = await acompletion(stream=True, **params)
              async for chunk in stream_iter:
                  # Robust extraction across providers
                  text = (
                      (chunk.get("choices", [{}])[0].get("delta", {}) or {}).get("content")
                      or chunk.get("content")
                      or getattr(chunk, "text", "")
                      or ""
                  )
                  if text:
                      sys.stdout.write(text)
                      sys.stdout.flush()
              click.echo()  # newline after stream
          else:
              resp = await acompletion(**params)
              content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
              click.echo(content)
      except KeyboardInterrupt:
          click.echo("", err=True)
          sys.exit(130)
      except Exception as e:
          logger.error(f"LLM call failed: {e}")
          sys.exit(1)

if __name__ == "__main__":
    main()
