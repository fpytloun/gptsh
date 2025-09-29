import asyncio
import sys
import click
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.core.mcp import list_tools

from typing import Any, Dict, Optional, List, cast, Mapping

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

        # Spinner helper (stderr) for progress indication
        async def _spinner(msg: str, stop_event: asyncio.Event) -> None:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while not stop_event.is_set():
                sys.stderr.write(f"\r{msg} {frames[i % len(frames)]}")
                sys.stderr.flush()
                i += 1
                try:
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    break
            # clear line
            sys.stderr.write("\r" + " " * (len(msg) + 2) + "\r")
            sys.stderr.flush()

        # shared spinner state to avoid redeclaration across branches
        spinner_task: Optional[asyncio.Task] = None
        stop_event: Optional[asyncio.Event] = None

        if stream:
            if progress and sys.stderr.isatty():
                stop_event = asyncio.Event()
                spinner_task = asyncio.create_task(_spinner("Progressing...", stop_event))
            try:
                stream_iter = await acompletion(stream=True, **params)
                async for chunk in stream_iter:
                    # Robust extraction across providers; handle Mapping-like and object chunks
                    def _extract_text(c: Any) -> str:
                        # 0) Direct string/bytes
                        if isinstance(c, (str, bytes)):
                            return c.decode() if isinstance(c, bytes) else c
                        # 1) Mapping-like (dict or implements get)
                        if isinstance(c, Mapping) or hasattr(c, "get"):
                            try:
                                m = c  # type: ignore[assignment]
                                # OpenAI-like
                                content = (
                                    (m.get("choices", [{}])[0].get("delta", {}) or {}).get("content")
                                )
                                if content:
                                    return str(content)
                                # Some providers put partial text under delta.text
                                delta = (m.get("choices", [{}])[0].get("delta", {}) or {})
                                text_val = delta.get("text") if isinstance(delta, Mapping) else None
                                if text_val:
                                    return str(text_val)
                                # Fallbacks
                                message = (m.get("choices", [{}])[0].get("message", {}) or {})
                                content = message.get("content") if isinstance(message, Mapping) else None
                                if content:
                                    return str(content)
                                if m.get("content"):
                                    return str(m.get("content"))
                                if m.get("text"):
                                    return str(m.get("text"))
                            except Exception:
                                pass
                        # 2) Attribute-based objects (e.g., litellm structured events)
                        try:
                            choices = getattr(c, "choices", None)
                            if choices:
                                first = choices[0] if len(choices) > 0 else None
                                if first is not None:
                                    delta = getattr(first, "delta", None)
                                    if delta is not None:
                                        content = getattr(delta, "content", None)
                                        if content:
                                            return str(content)
                                        text_val = getattr(delta, "text", None)
                                        if text_val:
                                            return str(text_val)
                            content_attr = getattr(c, "content", None)
                            if content_attr:
                                return str(content_attr)
                            text_attr = getattr(c, "text", None)
                            if text_attr:
                                return str(text_attr)
                        except Exception:
                            pass
                        return ""

                    text = _extract_text(chunk)
                    if text:
                        if spinner_task is not None and stop_event is not None and not stop_event.is_set():
                            stop_event.set()
                            await spinner_task
                            spinner_task = None
                        sys.stdout.write(text)
                        sys.stdout.flush()
            finally:
                if spinner_task is not None and stop_event is not None and not stop_event.is_set():
                    stop_event.set()
                    await spinner_task
            click.echo()  # newline after stream
        else:
            if progress and sys.stderr.isatty():
                stop_event = asyncio.Event()
                spinner_task = asyncio.create_task(_spinner("Progressing...", stop_event))
            try:
                resp = cast(Dict[str, Any], await acompletion(**params))
            finally:
                if spinner_task is not None and stop_event is not None and not stop_event.is_set():
                    stop_event.set()
                    await spinner_task
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
