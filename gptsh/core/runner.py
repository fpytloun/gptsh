from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

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
    - Detect fenced code blocks (``` or ~~~ with variable length). Accumulate entire fenced
      block and flush only when the closing fence arrives to avoid partial code rendering.
    - As a latency guard, if buffer grows beyond a threshold and ends with a newline,
      flush the current paragraph even without a double newline.
    - Always emit blocks terminated by at least a single trailing newline to prevent
      style bleed in Rich Markdown rendering.
    """

    def __init__(self, latency_chars: int = 1200) -> None:
        self._buf: str = ""
        self._in_fence: bool = False
        self._fence_marker: Optional[str] = None  # e.g., "```", "````", "~~~"
        self._latency_chars = latency_chars

    def _match_fence(self, line: str) -> Optional[str]:
        """Return the exact fence marker found at the start of the line (after optional indent).

        Supports variable-length ``` or ~~~ fences; returns the exact sequence, e.g. "````".
        """
        stripped = line.lstrip()
        if not stripped:
            return None
        ch = stripped[0]
        if ch not in ("`", "~"):
            return None
        # Count consecutive same characters from the start
        i = 0
        while i < len(stripped) and stripped[i] == ch:
            i += 1
        # At least 3 are required to form a fence
        if i >= 3:
            return stripped[:i]
        return None

    @staticmethod
    def _ensure_trailing_newline(block: str) -> str:
        # Ensure at least one trailing newline; prefer single to avoid adding extra empties
        if not block.endswith("\n"):
            return block + "\n"
        return block

    @staticmethod
    def _is_block_element_line(line: str) -> bool:
        """Check if a line is a block-level Markdown element.

        Detects: lists (unordered/ordered), blockquotes, horizontal rules, HTML blocks.
        """
        import re

        stripped = line.lstrip()
        if not stripped:
            return False

        first_char = stripped[0]

        # Unordered lists: -, *, +
        if first_char in ("-", "*", "+") and len(stripped) > 1 and stripped[1] == " ":
            return True

        # Ordered lists: 1. 2. etc.
        if first_char.isdigit():
            if re.match(r"^\d+\.\s", stripped):
                return True

        # Blockquotes: > text
        if first_char == ">":
            return True

        # Horizontal rules: ---, ***, ___ (at least 3, can have spaces between)
        if first_char in ("-", "*", "_"):
            if re.match(r"^([-*_])(\s*\1){2,}\s*$", stripped):
                return True

        # HTML/XML block start
        if first_char == "<":
            return True

        return False

    @staticmethod
    def _ends_with_block_element(text: str) -> bool:
        """Check if text block ends with a block-level element."""
        if not text:
            return False
        # Get the last non-empty line
        lines = text.rstrip("\n").split("\n")
        if not lines:
            return False
        last_line = lines[-1]
        return MarkdownBuffer._is_block_element_line(last_line)

    def push(self, chunk: str) -> List[str]:
        """Push text and return a list of complete markdown blocks ready to render."""
        out: List[str] = []
        self._buf += chunk

        # Process as much as possible
        while self._buf:
            if not self._in_fence:
                # If we can flush a full paragraph before any fence, do it
                # Find nearest paragraph boundary and nearest fence-start line
                par_idx = self._buf.find("\n\n")

                # Find fence start at any line-start
                fence_start_idx = -1
                scan_pos = 0
                while True:
                    # Determine start and end of the current line
                    if scan_pos == 0:
                        line_start = 0
                    else:
                        nl = self._buf.find("\n", scan_pos - 1)
                        if nl == -1:
                            break
                        line_start = nl + 1
                    next_nl = self._buf.find("\n", line_start)
                    if next_nl == -1:
                        # No full next line available yet
                        break
                    line = self._buf[line_start : next_nl + 1]
                    if self._match_fence(line):
                        fence_start_idx = line_start
                        break
                    scan_pos = next_nl + 1

                # If a paragraph boundary comes before any fence, flush up to boundary
                if par_idx != -1 and (fence_start_idx == -1 or par_idx < fence_start_idx):
                    block = self._buf[: par_idx + 2]
                    # If block ends with block-level element and next content exists without
                    # blank line, ensure proper separation
                    if self._ends_with_block_element(block):
                        next_content = self._buf[par_idx + 2 :]
                        if next_content and not next_content.startswith("\n"):
                            # Ensure block ends with blank line
                            if not block.endswith("\n\n"):
                                block = block.rstrip("\n") + "\n\n"
                    out.append(self._ensure_trailing_newline(block))
                    self._buf = self._buf[par_idx + 2 :]
                    continue

                # If a fence starts, emit any text before it and enter fence mode
                if fence_start_idx != -1:
                    before = self._buf[:fence_start_idx]
                    if before.strip():
                        out.append(self._ensure_trailing_newline(before))
                    self._buf = self._buf[fence_start_idx:]
                    # Determine exact opening fence marker
                    first_nl = self._buf.find("\n")
                    if first_nl == -1:
                        # Entire fence line not yet complete; wait for more
                        break
                    open_line = self._buf[: first_nl + 1]
                    marker = self._match_fence(open_line)
                    if marker:
                        self._in_fence = True
                        self._fence_marker = marker
                    # If somehow not a valid fence, just continue scanning on next loop
                    continue

                # No actionable boundary; stop for now
                break
            else:
                # Inside fence: look for closing fence line (>= opening length of same char)
                assert self._fence_marker is not None
                fence_char = self._fence_marker[0]
                fence_len = len(self._fence_marker)
                lines = self._buf.splitlines(keepends=True)
                acc = ""
                closed_index = -1
                for i, line in enumerate(lines):
                    acc += line
                    stripped = line.lstrip()
                    # A closing fence must not be the opening line (i > 0), must start with the
                    # fence char repeated >= opening length, and contain only optional whitespace after.
                    if i > 0 and stripped and stripped[0] == fence_char:
                        j = 0
                        while j < len(stripped) and stripped[j] == fence_char:
                            j += 1
                        # After the fence sequence, only spaces/tabs are allowed on closing lines
                        trailing = stripped[j:]
                        if j >= fence_len and (trailing.strip() == ""):
                            closed_index = i
                            break
                if closed_index != -1:
                    remainder = "".join(lines[closed_index + 1 :])
                    out.append(acc)  # Fenced block should be complete; keep as-is
                    self._buf = remainder
                    self._in_fence = False
                    self._fence_marker = None
                    continue
                # Not closed yet; wait for more chunks
                break

        # Latency guard: flush last paragraph if buffer is large and ends with newline
        if (
            not self._in_fence
            and len(self._buf) >= self._latency_chars
            and self._buf.endswith("\n")
        ):
            last_par = self._buf.rfind("\n\n")
            if last_par != -1:
                out.append(self._ensure_trailing_newline(self._buf[: last_par + 2]))
                self._buf = self._buf[last_par + 2 :]
            else:
                out.append(self._ensure_trailing_newline(self._buf))
                self._buf = ""

        return out

    def flush(self) -> Optional[str]:
        if not self._buf.strip():
            return None
        data = self._buf
        # If we are in a fence and it's not closed, auto-close to avoid broken rendering
        if self._in_fence and self._fence_marker:
            if not data.endswith("\n"):
                data += "\n"
            data += f"{self._fence_marker}\n"
        # Reset state
        self._buf = ""
        self._in_fence = False
        self._fence_marker = None
        return data


async def run_turn(
    *,
    agent: Any,
    user_message: Union[str, Dict[str, Any]],
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
            user_message=user_message,
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
                                # Ensure blank lines between blocks for proper separation
                                if block.endswith("\n\n"):
                                    # But not on separators as that would result in two empty lines
                                    if not block.startswith("---"):
                                        console.print("")
                else:
                    # Plain text: print whole lines only to avoid mid-line restarts
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        async with pr.aio_io():
                            console.print(line)

        # Print once in non-stream mode
        if not stream:
            final_text = full_output
            if final_text:
                async with pr.aio_io():
                    if output_format == "markdown":
                        console.print(Markdown(final_text))
                    else:
                        console.print(final_text)
            # Append to result sink for symmetry with stream path
            if result_sink is not None:
                try:
                    result_sink.append(full_output)
                except Exception:
                    pass
            return

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
    user_message: Union[str, Dict[str, Any]]  # String or full message dict with content array
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
        user_message=req.user_message,
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
        user_message=req.user_message,
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
