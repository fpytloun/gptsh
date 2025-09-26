import asyncio
import sys
import click
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.core.mcp import list_tools

# --- CLI Entrypoint ---

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--model", default=None, help="Override LLM model")
@click.option("--agent", default=None, help="Named agent preset from config")
@click.option("--config", "config_path", default=None, help="Specify alternate config path")
@click.option("--stream/--no-stream", default=True)
@click.option("--progress/--no-progress", default=True)
@click.option("--debug", is_flag=True, default=False)
@click.option("--list-tools", is_flag=True, default=False)
@click.argument("prompt", required=False)
def main(model, agent, config_path, stream, progress, debug, list_tools, prompt):
    """gptsh: Modular shell/LLM agent client."""
    # Load config
    # Load configuration: use custom path or defaults
    if config_path:
        config = load_config([config_path])
    else:
        config = load_config()
    log_level = "DEBUG" if debug else config.get("logging", {}).get("level", "INFO")
    log_fmt = config.get("logging", {}).get("format", "text")
    logger = setup_logging(log_level, log_fmt)
    
    if list_tools:
        tools_map = list_tools_stub()
        click.echo("Discovered tools:")
        for server, tools in tools_map.items():
            click.echo(f"{server}:")
            for tool in tools:
                click.echo(f"  - {tool}")
        sys.exit(0)
    # Handle prompt or stdin
    stdin_input = None
    if not prompt and not sys.stdin.isatty():
        stdin_input = read_stdin()
    # Try to get prompt from agent config if agent is set
    agent_conf = None
    agent_prompt = None
    if agent:
        agents_conf = config.get("agents", {})
        agent_conf = agents_conf.get(agent)
        if agent_conf:
            agent_prompt = agent_conf.get("prompt", {}).get("user")
    prompt_given = prompt or stdin_input or agent_prompt
    if prompt_given:
        asyncio.run(run_llm(prompt_given, config, model, agent, stream, logger))
    else:
        click.echo("Error: A prompt is required. Provide via CLI argument, stdin, or agent config's 'user' prompt.")
        sys.exit(2)

async def run_llm(prompt, config, model, agent, stream, logger):
    # Minimal async LLM call MVP (stub)
    try:
        from litellm import completion
        # Determine model: CLI override > agent preset > config > default
        if agent:
            agents_conf = config.get("agents", {})
            if agent not in agents_conf:
                logger.error(f"Agent '{agent}' not found in config.")
                sys.exit(2)
            agent_conf = agents_conf[agent]
            agent_model = agent_conf.get("model")
        else:
            agent_model = None
        chosen_model = model or agent_model or config.get("model") or "gpt-4.1"
        params = {"model": chosen_model, "messages": [{"role": "user", "content": prompt}]}
        logger.info(f"Calling LLM model {chosen_model}")
        # Invoke litellm completion with optional streaming
        if stream:
            # Stream tokens as they arrive
            async for chunk in completion.stream(**params):
                # chunk may contain partial text
                text = getattr(chunk, 'text', chunk.get('choices', [{}])[0].get('delta', {}).get('content', ''))
                click.echo(text, nl=False)
            click.echo()  # newline after stream
        else:
            resp = await completion(**params)
            click.echo(resp["choices"][0]["message"]["content"])
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
