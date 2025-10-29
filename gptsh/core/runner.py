from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown

from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.core.progress import NoOpProgressReporter
from gptsh.core.session import ChatSession
from gptsh.core.sessions import preload_session_to_chat, save_after_turn
from gptsh.interfaces import ProgressReporter
from gptsh.mcp.manager import MCPManager


class MarkdownBuffer:
    """Incremental Markdown block detector for streaming output.

    Heuristics:
    - Flush on blank-line paragraph boundaries ("\n\n") when not inside fenced code.
    - Detect fenced code blocks (``` or ~~~). Accumulate entire fenced block and flush
      only when the closing fence arrives to avoid partial code rendering.
    - As a latency guard, if buffer grows beyond a threshold and ends with a newline,
      flush the current paragraph even without a double newline.
    """

    def __init__(self, latency_chars: int = 1200) -> None:
        self._buf: str = ""
        self._in_fence: bool = False
        self._fence_marker: Optional[str] = None  # "```" or "~~~"
        self._latency_chars = latency_chars

    def _is_fence_line(self, line: str) -> Optional[str]:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            return "```"
        if stripped.startswith("~~~"):
            return "~~~"
        return None

    def push(self, chunk: str) -> List[str]:
        """Push text and return a list of complete markdown blocks ready to render."""
        out: List[str] = []
        self._buf += chunk

        # Fast path for fenced code blocks: emit only when closing fence appears
        cursor = 0
        while cursor < len(self._buf):
            if not self._in_fence:
                # Try to split at a paragraph boundary first
                idx = self._buf.find("\n\n", cursor)
                fence_idx = self._buf.find("```", cursor)
                fence_idx2 = self._buf.find("~~~", cursor)
                # Determine nearest fence start if any
                candidates = [i for i in [fence_idx, fence_idx2] if i != -1]
                nearest_fence = min(candidates) if candidates else -1

                if idx != -1 and (nearest_fence == -1 or idx < nearest_fence):
                    # We found a blank-line boundary before any fence; flush up to boundary
                    block = self._buf[: idx + 2]
                    out.append(block)
                    self._buf = self._buf[idx + 2 :]
                    cursor = 0
                    continue

                # Check if the buffer begins a fenced block
                # Look at start of lines up to possible next newline
                line_start = self._buf.rfind("\n", 0, cursor) + 1
                next_nl = self._buf.find("\n", cursor)
                if next_nl == -1:
                    break
                line = self._buf[line_start : next_nl + 1]
                mark = self._is_fence_line(line)
                if mark is not None:
                    # Flush any content preceding the fence if present
                    before = self._buf[:line_start]
                    if before.strip():
                        out.append(before)
                    # Enter fenced mode starting from fence line
                    self._buf = self._buf[line_start:]
                    self._in_fence = True
                    self._fence_marker = mark
                    cursor = 0
                    continue
                # No actionable boundary detected; break scan
                break
            else:
                # Inside fence: look for closing fence marker at start of a line
                close_idx = self._buf.find("\n" + (self._fence_marker or ""))
                # Also consider fence at very start
                start_close = self._buf.startswith(self._fence_marker or "")
                if close_idx != -1 or start_close:
                    # Find exact closing line
                    # Search line by line for a line that starts with the marker
                    lines = self._buf.splitlines(keepends=True)
                    acc = ""
                    closed = False
                    for i, line in enumerate(lines):
                        acc += line
                        # Closing fence line: must start with marker
                        if line.lstrip().startswith(self._fence_marker or "") and i != 0:
                            closed = True
                            # Include the fence line fully; then flush the fenced block
                            # and keep any remainder in buffer
                            # Append remaining lines after the fence back to buffer
                            remainder = "".join(lines[i + 1 :])
                            out.append(acc)
                            self._buf = remainder
                            self._in_fence = False
                            self._fence_marker = None
                            cursor = 0
                            break
                    if not closed:
                        # Not yet closed; wait for more chunks
                        break
                else:
                    # No closing fence yet
                    break

        # Latency guard: if buffer is long and ends with a newline, flush one paragraph
        if (
            not self._in_fence
            and len(self._buf) >= self._latency_chars
            and self._buf.endswith("\n")
        ):
            # Try to split at last blank line; otherwise flush entire buffer
            last_par = self._buf.rfind("\n\n")
            if last_par != -1:
                out.append(self._buf[: last_par + 2])
                self._buf = self._buf[last_par + 2 :]
            else:
                out.append(self._buf)
                self._buf = ""

        return out

    def flush(self) -> Optional[str]:
        if self._buf.strip():
            data = self._buf
            self._buf = ""
            self._in_fence = False
            self._fence_marker = None
            return data
        return None


async def run_turn(
    *,
    agent: Any,
    prompt: str,
    config: Dict[str, Any],
    stream: bool = True,
    output_format: str = "markdown",
    no_tools: bool = False,
    logger: Any = None,
    exit_on_interrupt: bool = True,
    result_sink: Optional[List[str]] = None,
    messages_sink: Optional[List[Dict[str, Any]]] = None,
    mcp_manager: Optional[MCPManager] = None,
    progress_reporter: ProgressReporter = None,  # always provided by caller (real or NoOp)
    session: Optional[ChatSession] = None,
) -> None:
    """Execute a single turn using an Agent with optional streaming and tools.

    This centralizes the CLI and REPL execution paths, including the streaming
    fallback when models stream tool_call deltas but produce no visible text.
    """
    pr: ProgressReporter = progress_reporter or NoOpProgressReporter()
    # Attach reporter to provided session so per-turn tasks render (REPL)
    try:
        if session is not None:
            session._progress = pr  # type: ignore[attr-defined]
    except Exception:
        pass
    console = Console()

    try:
        own_session = False
        if not session:
            session = ChatSession.from_agent(
                agent,
                progress=pr,
                config=config,
                mcp=(None if no_tools else (mcp_manager or MCPManager(config))),
            )
            own_session = True
            # Start background resources if we created the session
            await session.start()
        buffer = ""
        full_output = ""
        initial_hist_len = len(getattr(session, "history", []))
        mbuf: Optional[MarkdownBuffer] = MarkdownBuffer() if output_format == "markdown" else None

        async for text in session.stream_turn(
            prompt=prompt,
            no_tools=no_tools,
        ):
            if not text:
                continue

            full_output += text
            if stream:
                if output_format == "markdown" and mbuf is not None:
                    # Use smarter markdown buffering (paragraphs + fenced code blocks)
                    for block in mbuf.push(text):
                        if block.strip():
                            async with pr.aio_io():
                                console.print(Markdown(block))
                else:
                    # Plain text: print whole lines only to avoid mid-line restarts
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        async with pr.aio_io():
                            console.print(line)

        # Ensure any remaining buffered content is printed under IO guard
        if output_format == "markdown" and mbuf is not None:
            tail = mbuf.flush()
            if tail and tail.strip():
                async with pr.aio_io():
                    console.print(Markdown(tail))
        else:
            if buffer:
                async with pr.aio_io():
                    console.print(buffer)

        if result_sink is not None:
            try:
                result_sink.append(full_output)
            except Exception:
                pass

        if messages_sink is not None and hasattr(session, "history"):
            try:
                new_msgs = session.history[initial_hist_len:]
                messages_sink.extend(new_msgs)
            except Exception:
                pass
    except asyncio.TimeoutError:
        async with pr.aio_io():
            click.echo("Operation timed out", err=True)
        sys.exit(124)
    except ToolApprovalDenied as e:
        async with pr.aio_io():
            click.echo(f"Tool approval denied: {e}", err=True)
        sys.exit(4)
    except KeyboardInterrupt:
        if exit_on_interrupt:
            async with pr.aio_io():
                click.echo("", err=True)
            sys.exit(130)
        else:
            raise
    except asyncio.CancelledError:
        # Propagate task cancellation cleanly so callers (REPL) can handle it
        raise
    except Exception as e:  # pragma: no cover - defensive
        if logger is not None:
            try:
                logger.error(f"LLM call failed: {e}")
            except Exception:
                pass
        sys.exit(1)
    finally:
        # Close only sessions we created here; persistent sessions are managed by the caller
        try:
            if "own_session" in locals() and own_session and session is not None:
                await session.aclose()
        except Exception:
            pass


@dataclass
class RunRequest:
    agent: Any
    prompt: str
    config: Dict[str, Any]
    stream: bool = True
    output_format: str = "markdown"
    no_tools: bool = False
    logger: Any = None
    exit_on_interrupt: bool = True
    result_sink: Optional[List[str]] = None
    messages_sink: Optional[List[Dict[str, Any]]] = None
    mcp_manager: Optional[MCPManager] = None
    progress_reporter: ProgressReporter = None
    session: Optional[ChatSession] = None
    # Persistence-related (optional)
    session_doc: Optional[Dict[str, Any]] = None
    small_model: Optional[str] = None


async def run_turn_with_request(req: RunRequest) -> None:
    await run_turn(
        agent=req.agent,
        prompt=req.prompt,
        config=req.config,
        stream=req.stream,
        output_format=req.output_format,
        no_tools=req.no_tools,
        logger=req.logger,
        exit_on_interrupt=req.exit_on_interrupt,
        result_sink=req.result_sink,
        messages_sink=req.messages_sink,
        mcp_manager=req.mcp_manager,
        progress_reporter=req.progress_reporter,
        session=req.session,
    )


async def run_turn_with_persistence(req: RunRequest) -> None:
    """Run a turn and persist conversation to the provided session_doc.

    Expects req.session_doc to be provided (preloaded or newly created doc).
    """
    # Prepare a session and a messages sink for capturing deltas
    pr = req.progress_reporter or NoOpProgressReporter()
    created_session = False
    session = req.session
    if session is None:
        session = ChatSession.from_agent(
            req.agent,
            progress=pr,
            config=req.config,
            mcp=(None if req.no_tools else (req.mcp_manager or MCPManager(req.config))),
        )
        await session.start()
        created_session = True
    else:
        # Ensure provided session in REPL uses the current progress reporter
        try:
            session._progress = pr  # type: ignore[attr-defined]
        except Exception:
            pass

    # Preload existing history if a doc is provided
    if isinstance(req.session_doc, dict):
        try:
            preload_session_to_chat(req.session_doc, session)
        except Exception:
            pass

    messages_sink: List[Dict[str, Any]] = []

    # Run the turn with our prepared session
    await run_turn(
        agent=req.agent,
        prompt=req.prompt,
        config=req.config,
        stream=req.stream,
        output_format=req.output_format,
        no_tools=req.no_tools,
        logger=req.logger,
        exit_on_interrupt=req.exit_on_interrupt,
        result_sink=req.result_sink,
        messages_sink=messages_sink,
        mcp_manager=req.mcp_manager,
        progress_reporter=pr,
        session=session,
    )

    # Generate title if requested and not yet present
    try:
        if req.small_model:
            await session.ensure_title(req.small_model)  # type: ignore[attr-defined]
    except Exception:
        pass

    # Persist doc if provided
    if isinstance(req.session_doc, dict):
        try:
            save_after_turn(req.session_doc, session, messages_sink)
        except Exception:
            pass

    # Close only if we created it here
    if created_session:
        try:
            await session.aclose()
        except Exception:
            pass
