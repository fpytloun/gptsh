from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown

from gptsh.core.api import run_prompt_with_agent
from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.core.progress import RichProgressReporter
from gptsh.core.session import ChatSession


async def run_turn(
    *,
    agent: Any,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    stream: bool = True,
    progress: bool = True,
    output_format: str = "markdown",
    no_tools: bool = False,
    logger: Any = None,
    exit_on_interrupt: bool = True,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    result_sink: Optional[List[str]] = None,
) -> None:
    """Execute a single turn using an Agent with optional streaming and tools.

    This centralizes the CLI and REPL execution paths, including the streaming
    fallback when models stream tool_call deltas but produce no visible text.
    """
    pr: Optional[RichProgressReporter] = None
    console = Console()
    if progress and click.get_text_stream("stderr").isatty():
        pr = RichProgressReporter()
        pr.start()

    waiting_task_id: Optional[int] = None
    try:
        if stream:
            session = ChatSession.from_agent(agent, progress=pr, config=config)
            params, chosen_model = await session.prepare_stream(
                prompt=prompt,
                provider_conf=provider_conf,
                agent_conf=agent_conf,
                cli_model_override=cli_model_override,
                history_messages=history_messages,
            )
            try:
                import logging

                _logger = logging.getLogger(__name__)
                _logger.debug(
                    "Streaming with model=%s tools=%d choice=%s",
                    str(chosen_model).rsplit("/", 1)[-1],
                    len(params.get("tools") or []),
                    params.get("tool_choice"),
                )
            except Exception:
                pass
            wait_label = f"Waiting for {str(chosen_model).rsplit('/', 1)[-1]}"
            if pr is not None:
                waiting_task_id = pr.add_task(wait_label)
            md_buffer = "" if output_format == "markdown" else ""
            first_output_done = False
            full_output = ""
            async for text in session.stream_with_params(params):
                if not text:
                    continue
                if not first_output_done:
                    if pr is not None:
                        if waiting_task_id is not None:
                            pr.complete_task(waiting_task_id)
                            waiting_task_id = None
                        try:
                            pr.stop()
                        except Exception:
                            pass
                        # Ensure stdout starts on a fresh line after stopping progress
                        if output_format != "markdown":
                            try:
                                click.echo()
                            except Exception:
                                pass
                    first_output_done = True
                if output_format == "markdown":
                    md_buffer += text
                    while "\n" in md_buffer:
                        line, md_buffer = md_buffer.split("\n", 1)
                        console.print(Markdown(line))
                else:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                full_output += text
            # After stream ends: only print trailing newline for text if something was streamed
            if output_format == "markdown":
                if md_buffer:
                    console.print(Markdown(md_buffer))
            else:
                if first_output_done:
                    click.echo()
            # If we saw streamed tool deltas but no output, fallback to non-stream
            try:
                import logging

                info = getattr(session._llm, "get_last_stream_info", lambda: {})()  # type: ignore[attr-defined]
                if isinstance(info, dict):
                    if not full_output and info.get("saw_tool_delta"):
                        logging.getLogger(__name__).debug(
                            "Stream ended with no text but tool deltas were observed: %s",
                            info.get("tool_names"),
                        )
                        # Pause progress before printing fallback content, will resume before tool loop
                        content = await run_prompt_with_agent(
                            agent=agent,
                            prompt=prompt,
                            config=config,
                            provider_conf=provider_conf,
                            agent_conf=agent_conf,
                            cli_model_override=cli_model_override,
                            no_tools=False,
                            history_messages=history_messages,
                            progress_reporter=pr,
                        )
                        if pr is not None:
                            try:
                                pr.stop()
                            except Exception:
                                pass
                        if output_format == "markdown":
                            console.print(Markdown(content or ""))
                        else:
                            # Ensure separation from any prior progress output
                            try:
                                click.echo()
                            except Exception:
                                pass
                            click.echo(content or "")
                        if result_sink is not None:
                            try:
                                result_sink.append(content or "")
                            except Exception:
                                pass
                        return
            except ToolApprovalDenied as e:
                click.echo(f"Tool approval denied: {e}", err=True)
                sys.exit(4)
            except Exception:
                pass
            if result_sink is not None:
                try:
                    result_sink.append(full_output)
                except Exception:
                    pass
        else:
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
                content = await run_prompt_with_agent(
                    agent=agent,
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
                if waiting_task_id is not None and pr is not None:
                    pr.complete_task(waiting_task_id)
                    waiting_task_id = None
                click.echo(f"Tool approval denied: {e}", err=True)
                sys.exit(4)
            if result_sink is not None:
                try:
                    result_sink.append(content or "")
                except Exception:
                    pass
            if output_format == "markdown":
                console.print(Markdown(content or ""))
            else:
                click.echo(content or "")
    except asyncio.TimeoutError:
        click.echo("Operation timed out", err=True)
        sys.exit(124)
    except KeyboardInterrupt:
        if exit_on_interrupt:
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
        if waiting_task_id is not None and pr is not None:
            pr.complete_task(waiting_task_id)
            waiting_task_id = None
        if pr is not None:
            try:
                pr.stop()
            except Exception:
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
    progress: bool = True
    output_format: str = "markdown"
    no_tools: bool = False
    logger: Any = None
    exit_on_interrupt: bool = True
    history_messages: Optional[List[Dict[str, Any]]] = None
    result_sink: Optional[List[str]] = None


async def run_turn_with_request(req: RunRequest) -> None:
    await run_turn(
        agent=req.agent,
        prompt=req.prompt,
        config=req.config,
        provider_conf=req.provider_conf,
        agent_conf=req.agent_conf,
        cli_model_override=req.cli_model_override,
        stream=req.stream,
        progress=req.progress,
        output_format=req.output_format,
        no_tools=req.no_tools,
        logger=req.logger,
        exit_on_interrupt=req.exit_on_interrupt,
        history_messages=req.history_messages,
        result_sink=req.result_sink,
    )
