from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown

from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.core.progress import RichProgressReporter
from gptsh.core.session import ChatSession
from gptsh.mcp.manager import MCPManager


async def run_turn(
    *,
    agent: Any,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    stream: bool = True,
    output_format: str = "markdown",
    no_tools: bool = False,
    logger: Any = None,
    exit_on_interrupt: bool = True,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    result_sink: Optional[List[str]] = None,
    messages_sink: Optional[List[Dict[str, Any]]] = None,
    mcp_manager: Optional[MCPManager] = None,
    progress_reporter: Optional[RichProgressReporter] = None,
) -> None:
    """Execute a single turn using an Agent with optional streaming and tools.

    This centralizes the CLI and REPL execution paths, including the streaming
    fallback when models stream tool_call deltas but produce no visible text.
    """
    pr: Optional[RichProgressReporter] = progress_reporter
    console = Console()

    try:
        session = ChatSession.from_agent(
            agent,
            progress=pr,
            config=config,
            mcp=(None if no_tools else (mcp_manager or MCPManager(config))),
        )
        buffer = ""
        full_output = ""
        initial_hist_len = len(history_messages) if isinstance(history_messages, list) else None

        async for text in session.stream_turn(
            prompt=prompt,
            provider_conf=provider_conf,
            agent_conf=agent_conf,
            cli_model_override=cli_model_override,
            no_tools=no_tools,
            history_messages=history_messages,
        ):
            if not text:
                continue

            full_output += text
            buffer += text

            if stream:
                if pr is not None:
                    if output_format == "markdown":
                        # Print full paragraphs only
                        while "\n\n" in buffer:
                            line, buffer = buffer.split("\n\n", 1)
                            async with pr.aio_io():
                                console.print(Markdown(line))
                    else:
                        # Only print full lines to avoid mid-line restarts
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            async with pr.aio_io():
                                console.print(line)
                else:
                    if output_format == "markdown":
                        while "\n\n" in buffer:
                            line, buffer = buffer.split("\n\n", 1)
                            console.print(Markdown(line))
                    else:
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            console.print(line)

        # Ensure any remaining buffered content is printed under IO guard

        if pr is not None:
            async with pr.aio_io():
                if output_format == "markdown":
                    if buffer:
                        console.print(Markdown(buffer))
                else:
                    if buffer:
                        console.print(buffer)
        else:
            if output_format == "markdown":
                if buffer:
                    console.print(Markdown(buffer))
            else:
                if buffer:
                    console.print(buffer)

        # If we saw streamed tool deltas but no output, fallback to non-stream
        # stream_turn already executed tools and finalized output.

        if result_sink is not None:
            try:
                result_sink.append(full_output)
            except Exception:
                pass

        # Populate structured message deltas if requested and history was provided
        if messages_sink is not None and isinstance(history_messages, list) and initial_hist_len is not None:
            try:
                new_msgs = history_messages[initial_hist_len:]
                messages_sink.extend(new_msgs)
            except Exception:
                pass
    except asyncio.TimeoutError:
        if pr is not None:
            async with pr.aio_io():
                click.echo("Operation timed out", err=True)
        else:
            click.echo("Operation timed out", err=True)
        sys.exit(124)
    except ToolApprovalDenied as e:
        if pr is not None:
            async with pr.aio_io():
                click.echo(f"Tool approval denied: {e}", err=True)
        else:
            click.echo(f"Tool approval denied: {e}", err=True)
        sys.exit(4)
    except KeyboardInterrupt:
        if exit_on_interrupt:
            if pr is not None:
                async with pr.aio_io():
                    click.echo("", err=True)
            else:
                click.echo("", err=True)
            sys.exit(130)
        else:
            raise
    except Exception as e:  # pragma: no cover - defensive
        if logger is not None:
            try:
                logger.error(f"LLM call failed: {e}")
            except Exception:
                pass
        sys.exit(1)
    finally:
        # Do not stop here; lifecycle is managed by the caller (CLI entrypoint/REPL)
        pass


@dataclass
class RunRequest:
    agent: Any
    prompt: str
    config: Dict[str, Any]
    provider_conf: Dict[str, Any]
    agent_conf: Optional[Dict[str, Any]] = None
    cli_model_override: Optional[str] = None
    stream: bool = True
    output_format: str = "markdown"
    no_tools: bool = False
    logger: Any = None
    exit_on_interrupt: bool = True
    history_messages: Optional[List[Dict[str, Any]]] = None
    result_sink: Optional[List[str]] = None
    messages_sink: Optional[List[Dict[str, Any]]] = None
    mcp_manager: Optional[MCPManager] = None
    progress_reporter: Optional[RichProgressReporter] = None


async def run_turn_with_request(req: RunRequest) -> None:
    await run_turn(
        agent=req.agent,
        prompt=req.prompt,
        config=req.config,
        provider_conf=req.provider_conf,
        agent_conf=req.agent_conf,
        cli_model_override=req.cli_model_override,
        stream=req.stream,
        output_format=req.output_format,
        no_tools=req.no_tools,
        logger=req.logger,
        exit_on_interrupt=req.exit_on_interrupt,
        history_messages=req.history_messages,
        result_sink=req.result_sink,
        messages_sink=req.messages_sink,
        mcp_manager=req.mcp_manager,
        progress_reporter=req.progress_reporter,
    )

