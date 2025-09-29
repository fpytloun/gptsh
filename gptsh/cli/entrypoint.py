import asyncio
import sys
import json
import click
from gptsh.config.loader import load_config
from gptsh.core.logging import setup_logging
from gptsh.core.stdin_handler import read_stdin
from gptsh.mcp import list_tools, get_auto_approved_tools, discover_tools_detailed_async, execute_tool_async
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from rich.markdown import Markdown

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
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging (INFO)")
@click.option("--mcp-servers", "mcp_servers", default=None, help="Override path to MCP servers file")
@click.option("--list-tools", "list_tools_flag", is_flag=True, default=False)
@click.option("--list-providers", "list_providers_flag", is_flag=True, default=False, help="List configured providers")
@click.option("--output", "-o", type=click.Choice(["text", "markdown"]), default="markdown", help="Output format")
@click.option("--no-tools", is_flag=True, default=False, help="Disable MCP tools (discovery and execution)")
@click.option("--tools", "tools_filter", default=None, help="Comma/space-separated MCP server labels to allow (others skipped)")
@click.argument("prompt", required=False)
def main(provider, model, agent, config_path, stream, progress, debug, verbose, mcp_servers, list_tools_flag, list_providers_flag, output, no_tools, tools_filter, prompt):
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
        approved_map = get_auto_approved_tools(config)
        total_servers = len(tools_map)
        click.echo(f"Discovered tools ({total_servers} server{'s' if total_servers != 1 else ''}):")
        for server, tools in tools_map.items():
            approved_set = set(approved_map.get(server, []))
            click.echo(f"{server} ({len(tools)}):")
            if tools:
                for tool in tools:
                    badge = " 󰁪" if tool in approved_set else ""
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
            output_format=output,
            no_tools=no_tools,
            logger=logger,
        ))
    else:
        raise click.UsageError("A prompt is required. Provide via CLI argument, stdin, or agent config's 'user' prompt.")

async def _build_mcp_tools_for_llm(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build OpenAI-style tool specs from MCP tool discovery.
    Tool names are prefixed with '<server>__' to route calls back.
    """
    tools: List[Dict[str, Any]] = []
    detailed = await discover_tools_detailed_async(config)
    for server, items in detailed.items():
        for t in items:
            name = f"{server}__{t['name']}"
            description = t.get("description") or ""
            params = t.get("input_schema") or {"type": "object", "properties": {}, "additionalProperties": True}
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            })
    return tools

def _parse_tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract tool_calls from a LiteLLM-normalized response.
    """
    calls: List[Dict[str, Any]] = []
    try:
        choice0 = (resp.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        tcalls = msg.get("tool_calls") or []
        # Normalize
        for c in tcalls:
            f = c.get("function") or {}
            name = f.get("name")
            arguments = f.get("arguments")
            call_id = c.get("id")
            if name:
                calls.append({"id": call_id, "name": name, "arguments": arguments})
    except Exception:
        pass
    return calls

async def run_llm(
      prompt: str,
      provider_conf: Dict[str, Any],
      agent_conf: Optional[Dict[str, Any]],
      cli_model_override: Optional[str],
      stream: bool,
      progress: bool,
      output_format: str,
      no_tools: bool,
      logger: Any,
  ) -> None:
    """Execute an LLM call using LiteLLM with optional streaming."""
    try:
        from litellm import acompletion

        # Build base params from provider configuration, excluding non-LiteLLM keys
        params: Dict[str, Any] = {
            k: v for k, v in dict(provider_conf).items() if k not in {"model", "name"}
        }

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

        # Build MCP tools for the LLM unless disabled
        if no_tools:
            mcp_tools = []
            # Establish a global_conf variable for later tool execution paths (kept empty here)
            global_conf = {}
        else:
            if progress_obj is not None:
                init_task_id = progress_obj.add_task("Initializing MCP tools", total=None)
            try:
                # Merge global MCP config (including allowed_servers) with provider/agent overrides
                try:
                    from inspect import currentframe
                    outer_locals = currentframe().f_back.f_back.f_locals  # main()'s locals (contains 'config')
                    global_conf0 = outer_locals.get("config", {}) or {}
                except Exception:
                    global_conf0 = {}
                merged_conf = {
                    "mcp": {
                        **(global_conf0.get("mcp", {}) or {}),
                        **(provider_conf.get("mcp", {}) or {}),
                        **(((agent_conf or {}).get("mcp", {})) or {}),
                    }
                }
                mcp_tools = await _build_mcp_tools_for_llm(merged_conf)
            finally:
                if 'init_task_id' in locals() and progress_obj is not None:
                    try:
                        progress_obj.remove_task(init_task_id)
                    except Exception:
                        pass
            # If no mcp section in provider/agent, fall back to global config already loaded in main()
            # The CLI stores resolved 'mcp.servers_files' in the global config; access through closure
            try:
                from inspect import currentframe
                outer_locals = currentframe().f_back.f_back.f_locals  # main()'s locals (contains 'config')
                global_conf = outer_locals.get("config", {})
            except Exception:
                global_conf = {}
            if not mcp_tools:
                if progress_obj is not None:
                    init_task_id2 = progress_obj.add_task("Initializing MCP tools", total=None)
                try:
                    mcp_tools = await _build_mcp_tools_for_llm(global_conf)
                finally:
                    if 'init_task_id2' in locals() and progress_obj is not None:
                        try:
                            progress_obj.remove_task(init_task_id2)
                        except Exception:
                            pass

        # If MCP tools are present, we'll do a tool-execution loop; disable streaming for compatibility
        if mcp_tools:
            stream = False

        # Determine model: CLI override > agent config > provider default > fallback
        chosen_model = (
            cli_model_override
            or (agent_conf or {}).get("model")
            or provider_conf.get("model")
            or "gpt-4o"
        )

        # Build messages: system then user
        messages: List[Dict[str, Any]] = []
        system_prompt = (agent_conf or {}).get("prompt", {}).get("system")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        params["model"] = chosen_model
        params["messages"] = messages
        if mcp_tools:
            params["tools"] = mcp_tools
            params["tool_choice"] = "auto"

        logger.info(f"Calling LLM model {chosen_model}")

        # Using rich Progress for progress display
        waiting_task_id: Optional[int] = None

        if stream:
            if progress_obj is not None:
                waiting_task_id = progress_obj.add_task(f"Waiting for {chosen_model.rsplit('/', 1)[-1]}", total=None)
            try:
                stream_iter = await acompletion(stream=True, **params)
                first_output_done = False
                buffer_md: List[str] = [] if output_format == "markdown" else []
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
                            buffer_md.append(text)
                        else:
                            sys.stdout.write(text)
                            sys.stdout.flush()
            finally:
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
            if output_format == "markdown":
                console.print(Markdown("".join(buffer_md)))
            else:
                click.echo()  # newline after stream
        else:
            if progress_obj is not None:
                waiting_task_id = progress_obj.add_task(f"Waiting for {chosen_model.rsplit('/', 1)[-1]}", total=None)
            try:
                # Tool execution loop when MCP tools are available
                if params.get("tools"):
                    conversation: List[Dict[str, Any]] = list(messages)
                    max_iters = 5
                    for _ in range(max_iters):
                        params["messages"] = conversation
                        resp = cast(Dict[str, Any], await acompletion(**params))
                        calls = _parse_tool_calls(resp)
                        if not calls:
                            # No tool calls; print final assistant message
                            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
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
                            break
                        # Append the assistant message that contains tool_calls (required by OpenAI format)
                        assistant_tool_calls: List[Dict[str, Any]] = []
                        for c in calls:
                            fullname_c = c["name"]
                            argstr_c = c.get("arguments")
                            if not isinstance(argstr_c, str):
                                try:
                                    argstr_c = json.dumps(argstr_c or {})
                                except Exception:
                                    argstr_c = "{}"
                            assistant_tool_calls.append({
                                "id": c.get("id"),
                                "type": "function",
                                "function": {
                                    "name": fullname_c,
                                    "arguments": argstr_c,
                                },
                            })
                        conversation.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": assistant_tool_calls,
                        })
                        # Execute each tool call and append tool results
                        for call in calls:
                            fullname = call["name"]
                            # Split "server__tool"
                            if "__" in fullname:
                                server, toolname = fullname.split("__", 1)
                            else:
                                # If not prefixed, assume single server context not supported; skip gracefully
                                continue
                            # Parse arguments
                            args_str = call.get("arguments") or "{}"
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
                            except Exception:
                                args = {}
                            # Temporarily change progress message to show executing tool
                            exec_task_id = None
                            if progress_obj is not None:
                                if waiting_task_id is not None:
                                    try:
                                        progress_obj.update(waiting_task_id, visible=False)
                                    except Exception:
                                        pass
                                exec_task_id = progress_obj.add_task(f"Executing {fullname}", total=None)
                            try:
                                result = await execute_tool_async(server, toolname, args, global_conf)
                            except Exception as e:
                                result = f"Tool execution failed: {e}"
                            finally:
                                if exec_task_id is not None and progress_obj is not None:
                                    try:
                                        progress_obj.remove_task(exec_task_id)
                                    except Exception:
                                        pass
                                # Restore waiting indicator
                                if progress_obj is not None and waiting_task_id is not None:
                                    try:
                                        progress_obj.update(waiting_task_id, visible=True)
                                    except Exception:
                                        pass
                            # Append tool result
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": fullname,
                                "content": result,
                            })
                        # Continue loop for potential follow-up tool calls
                    else:
                        # Max iterations reached; stop
                        pass
                else:
                    resp = cast(Dict[str, Any], await acompletion(**params))
                    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
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
            finally:
                # Ensure waiting indicator is cleared
                if waiting_task_id is not None and progress_obj is not None:
                    try:
                        progress_obj.remove_task(waiting_task_id)
                    except Exception:
                        pass
                    waiting_task_id = None
                # Stop progress display if it was started
                if progress_obj is not None and progress_running:
                    try:
                        progress_obj.stop()
                    except Exception:
                        pass
                    progress_running = False
    except KeyboardInterrupt:
        click.echo("", err=True)
        sys.exit(130)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
