from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from gptsh.core.agent import Agent
from gptsh.core.config_api import compute_tools_policy
from gptsh.core.exceptions import ReplExit
from gptsh.core.sessions import (
    load_session as _load_session,
    new_session_doc as _new_session_doc,
    resolve_session_ref as _resolve_session_ref,
    resolve_small_model as _resolve_small_model,
)
from gptsh.mcp import ensure_sessions_started_async as ensure_sessions_started_async  # noqa: F401

_log = logging.getLogger(__name__)


def build_prompt(
    *,
    agent_name: Optional[str],
    model: Optional[str],
    template: Optional[str] = None,
) -> ANSI:
    """Build the REPL prompt with optional templating.

    Args:
        agent_name: Name of the agent
        model: Model identifier
        template: Optional format template with placeholders:
                  {agent} - colored agent name
                  {model} - colored model name
                  {agent_plain} - plain agent name
                  {model_plain} - plain model name
                  Defaults to "{agent}|{model}> "
    """
    model_label = str(model or "?").rsplit("/", 1)[-1]
    agent_label = agent_name or "default"
    agent_col = click.style(agent_label, fg="cyan", bold=True)
    model_col = click.style(model_label, fg="magenta")

    # Use provided template or default
    if template is None:
        template = "{agent}|{model}> "

    # Format the template
    prompt_text = template.format(
        agent=agent_col,
        model=model_col,
        agent_plain=agent_label,
        model_plain=model_label,
    )

    # Return as ANSI so prompt_toolkit handles color codes properly
    return ANSI(prompt_text)


def command_exit() -> None:
    raise ReplExit()


def command_model(
    arg: Optional[str],
    *,
    agent: Agent,
    agent_name: Optional[str],
    template: Optional[str] = None,
) -> Tuple[str, ANSI]:
    if not arg:
        raise ValueError("Usage: /model <model>")
    new_model = arg.strip()
    agent.llm._base["model"] = new_model
    prompt_str = build_prompt(
        agent_name=agent_name,
        model=new_model,
        template=template,
    )
    return new_model, prompt_str


def command_reasoning_effort(arg: Optional[str], agent: Agent):
    if not arg:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    val = arg.strip().lower()
    if val not in {"minimal", "low", "medium", "high"}:
        raise ValueError("Usage: /reasoning_effort [minimal|low|medium|high]")
    agent.llm._base["reasoning_effort"] = val


def command_info(agent: Agent) -> str:
    """Return a human-readable session/model info string.

    Includes agent name, effective model, key parameters, usage (tokens/cost),
    and context window with usage percentage using litellm.get_max_tokens.
    """
    model = agent.llm._base.get("model", "?")

    # Pull session usage from CLI cache if available
    session = agent.session

    usage: Dict = {}
    if session:
        # Session might not be initialized yet
        usage = session.usage

    tokens = usage.get("tokens", {}) or {}
    prompt_t = tokens.get("prompt")
    completion_t = tokens.get("completion")
    total_t = tokens.get("total")
    cached_t = tokens.get("cached_tokens")
    reasoning_t = tokens.get("reasoning_tokens")
    cost = usage.get("cost")

    # Determine max context via litellm.get_max_tokens
    max_ctx = None
    try:
        from litellm.utils import _get_model_info_helper  # type: ignore

        info = _get_model_info_helper(model=model) or {}
        max_ctx = info.get("max_input_tokens")
    except Exception:
        max_ctx = None

    pct = None
    try:
        if isinstance(total_t, (int, float)) and isinstance(max_ctx, int) and max_ctx > 0:
            pct = (float(total_t) / float(max_ctx)) * 100.0
    except Exception:
        pct = None

    params_parts: List[str] = []
    tval = agent.llm._base.get("temperature")
    if tval is not None:
        params_parts.append(f"temperature={tval}")
    reff = agent.llm._base.get("reasoning_effort")
    if reff is not None:
        params_parts.append(f"reasoning_effort={reff}")
    params_str = (", ".join(params_parts)) if params_parts else "(default)"

    lines: List[str] = []
    lines.append(f"Model: {model}")
    lines.append(f"Parameters: {params_str}")
    if any(v is not None for v in [prompt_t, completion_t, reasoning_t, total_t, cached_t, cost]):
        lines.append("Session usage:")
        if prompt_t is not None:
            lines.append(f"  - prompt tokens: {prompt_t}")
        if completion_t is not None:
            lines.append(f"  - completion tokens: {completion_t}")
        if reasoning_t is not None:
            lines.append(f"  - reasoning tokens: {reasoning_t}")
        if total_t is not None:
            lines.append(f"  - total tokens: {total_t}")
        if cached_t is not None:
            lines.append(f"  - cached tokens: {cached_t}")
        if cost is not None:
            lines.append(f"  - total cost: ${cost:.5f}")
    else:
        lines.append("Usage: (no usage recorded yet in this session)")
    if max_ctx is not None:
        if pct is not None:
            lines.append(f"Context window: {total_t or 0} / {max_ctx} tokens (~{pct:.1f}%)")
        else:
            lines.append(f"Context window: {max_ctx} tokens")
    else:
        lines.append("Context window: (unknown)")

    return "\n".join(lines)


def command_tools(agent: Any) -> str:
    """Return a formatted list of tools for the current agent.

    Output matches the CLI list format: server (count):\n  - tool
    """
    tools_map = getattr(agent, "tools", {}) or {}
    if not tools_map:
        return "(no tools discovered)"
    lines: List[str] = []
    policy = getattr(agent, "policy", None)
    for server, handles in tools_map.items():
        lines.append(f"{server} ({len(handles)}):")
        for h in handles:
            name = getattr(h, "name", "?")
            badge = ""
            try:
                if policy and policy.is_auto_allowed(server, name):
                    badge = " \u2714"  # checkmark for auto-approved
            except Exception as e:
                _log.debug("policy.is_auto_allowed failed for %s/%s: %s", server, name, e)
            lines.append(f"  - {name}{badge}")
    return "\n".join(lines)


def command_no_tools(
    arg: Optional[str],
    *,
    config: Dict[str, Any],
    agent_name: str,
    cli_model_override: Optional[str],
    current_no_tools: bool,
) -> tuple[Any, bool, str]:
    """Toggle or set no-tools and return (new_agent, no_tools, message).

    - arg: "on" to disable tools, "off" to enable tools, None/"" to toggle.
    - Rebuilds the Agent via build_agent to reflect the new policy.
    """
    val = (arg or "").strip().lower()
    if val not in {"", "on", "off"}:
        raise ValueError("Usage: /no-tools [on|off]")
    if val == "on":
        effective_no_tools = True
    elif val == "off":
        effective_no_tools = False
    else:
        effective_no_tools = not current_no_tools
    import threading

    from gptsh.core.config_resolver import build_agent as _build_agent

    result_box: Dict[str, Any] = {}

    def _worker():  # pragma: no cover - thread setup
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_box["agent"] = loop.run_until_complete(
                _build_agent(
                    config,
                    cli_agent=agent_name,
                    cli_provider=None,
                    cli_tools_filter=None,
                    cli_model_override=cli_model_override,
                    cli_no_tools=effective_no_tools,
                )
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    new_agent = result_box.get("agent")
    tools_map = getattr(new_agent, "tools", {}) or {}
    msg = f"Tools {'disabled' if effective_no_tools else 'enabled'} ({sum(len(v or []) for v in tools_map.values())} available)"
    return new_agent, effective_no_tools, msg


def command_agent(
    arg: Optional[str],
    *,
    config: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]],
    agent_name: Optional[str],
    provider_conf: Dict[str, Any],
    cli_model_override: Optional[str],
    no_tools: bool,
    mgr: Any,
    loop: Any,
    template: Optional[str] = None,
) -> Tuple[Dict[str, Any], ANSI, str, bool, Any]:
    if not arg:
        raise ValueError("Usage: /agent <agent>")
    new_agent = arg.strip()
    agents_conf_all = config.get("agents") or {}
    if new_agent not in agents_conf_all:
        raise ValueError(f"Unknown agent '{new_agent}'")
    agent_conf = agents_conf_all.get(new_agent) or {}
    agent_name = new_agent
    cli_model_override = agent_conf.get("model") if isinstance(agent_conf, dict) else None
    labels = None
    no_tools, allowed = compute_tools_policy(agent_conf, labels, False)
    mcp_cfg = config.setdefault("mcp", {})
    if allowed is not None:
        mcp_cfg["allowed_servers"] = allowed
    else:
        mcp_cfg.pop("allowed_servers", None)
    try:
        nonce = (mcp_cfg.get("_repl_nonce") or 0) + 1
        mcp_cfg["_repl_nonce"] = nonce
    except Exception as e:
        _log.debug("Failed to bump MCP nonce: %s", e)
        mcp_cfg["_repl_nonce"] = 1
    mgr = None
    prompt_str = build_prompt(
        agent_name=agent_name,
        model=cli_model_override,
        template=template,
    )
    return agent_conf, prompt_str, agent_name, no_tools, mgr


async def command_file(
    arg: Optional[str], agent: Agent, config: Dict[str, Any]
) -> Union[str, Dict[str, Any]]:
    """Load a file attachment.

    Supports:
    - Small UTF-8 text files (≤64KB): return inline content with truncation notice if needed.
    - Audio files: transcribe if possible, otherwise return as multimodal audio content.
    - Binary or large files: return a concise marker.

    Returns:
    - str: Text content for text files or fallback markers
    - dict: {"type": "audio", "content": str_marker, "attachment": {...}} for audio files
            that couldn't be transcribed (for multimodal support)

    CRITICAL: Uses is_probably_text() to prevent any binary from being inlined.
    """
    if not arg:
        raise ValueError("Usage: /file <path>")

    from pathlib import Path

    from gptsh.core.stdin_handler import is_probably_text, sniff_mime
    from gptsh.core.transcribe import transcribe_audio

    path = Path(arg.strip()).expanduser()
    if not path.is_file():
        raise ValueError(f"File not found or not accessible: {path}")

    max_inline = 65536  # 64KB
    try:
        size = path.stat().st_size
        if size > max_inline:
            # Too large: read header only for MIME detection
            try:
                with open(path, "rb") as f:
                    head = f.read(512)
                mime = sniff_mime(head)
            except Exception:
                mime = "application/octet-stream"

            # For images and PDFs, read full file and return multimodal structure
            if mime.startswith("image/"):
                try:
                    with open(path, "rb") as f:
                        image_data = f.read()
                    return {
                        "type": "image",
                        "mime": mime,
                        "data": image_data,
                        "truncated": False,
                        "name": path.name,
                    }
                except Exception:
                    pass  # Fall through to marker

            if mime == "application/pdf":
                try:
                    with open(path, "rb") as f:
                        pdf_data = f.read()
                    return {
                        "type": "pdf",
                        "mime": mime,
                        "data": pdf_data,
                        "truncated": False,
                        "name": path.name,
                    }
                except Exception:
                    pass  # Fall through to marker

            # Try to transcribe if audio and not too large for Whisper API
            if mime.startswith("audio/") and size <= 25000000:
                try:
                    with open(path, "rb") as f:
                        audio_data = f.read()
                    transcript = await transcribe_audio(audio_data, mime, config)
                    if transcript:
                        return f"File: {path.name} (audio transcribed)\n\n{transcript}"
                    # Transcription failed/disabled - return as multimodal audio
                    return {
                        "type": "audio",
                        "content": f"File: {path.name} (audio, {size} bytes)",
                        "attachment": {
                            "type": "audio",
                            "mime": mime,
                            "data": audio_data,
                            "truncated": False,
                        },
                    }
                except Exception:
                    pass  # Fall through to marker

            return f"[Attached file: {path.name} ({mime}, {size} bytes)]"

        # Read full content for analysis
        with open(path, "rb") as f:
            data = f.read(max_inline + 1)

        truncated = len(data) > max_inline
        if truncated:
            data = data[:max_inline]

        mime = sniff_mime(data)

        # Try to transcribe audio files
        if mime.startswith("audio/"):
            try:
                # Read full file if needed (under size limit)
                if not truncated:
                    audio_data = data
                else:
                    with open(path, "rb") as f:
                        audio_data = f.read()
                transcript = await transcribe_audio(audio_data, mime, config)
                if transcript:
                    return f"File: {path.name} (audio transcribed)\n\n{transcript}"
                # Transcription failed/disabled - return as multimodal audio
                return {
                    "type": "audio",
                    "content": f"File: {path.name} (audio, {len(audio_data)} bytes)",
                    "attachment": {
                        "type": "audio",
                        "mime": mime,
                        "data": audio_data,
                        "truncated": truncated,
                    },
                }
            except Exception:
                pass  # Fall through to marker

        # Handle different content types
        if mime == "text/plain" and is_probably_text(data):
            # Plain text: inline content
            try:
                text = data.decode("utf-8", errors="strict")
                if truncated:
                    text += "\n[...File truncated. Content exceeded 64KB.]"
                return f"File: {path.name}\n{text}"
            except UnicodeDecodeError:
                pass
        elif mime.startswith("image/"):
            # Image: return multimodal structure for build_user_message
            return {
                "type": "image",
                "mime": mime,
                "data": data,
                "truncated": truncated,
                "name": path.name,
            }
        elif mime == "application/pdf":
            # PDF: return multimodal structure for build_user_message
            return {
                "type": "pdf",
                "mime": mime,
                "data": data,
                "truncated": truncated,
                "name": path.name,
            }

        # Everything else: binary marker
        return f"[Attached file: {path.name} ({mime}, {size} bytes)]"

    except Exception as e:
        raise ValueError(f"Failed to read file: {e}") from e


def command_copy(agent: Agent) -> str:
    """Copy the last assistant message to clipboard.

    Returns:
        str: Status message for user feedback

    Raises:
        ValueError: If no assistant message found or copy fails
    """
    import json

    from gptsh.core.multimodal import message_to_text
    from gptsh.mcp.builtin.clipboard import _tool_clipboard_write

    sess = getattr(agent, "session", None)
    if sess is None or not sess.history:
        raise ValueError("No messages in session history yet")

    # Find last assistant message (iterate backwards)
    last_assistant_msg = None
    for msg in reversed(sess.history):
        if msg.get("role") == "assistant":
            last_assistant_msg = msg
            break

    if last_assistant_msg is None:
        raise ValueError("No assistant message found in history")

    # Extract text content (handles both string and multimodal)
    content = last_assistant_msg.get("content")
    if isinstance(content, list):
        # Multimodal content - extract text parts only
        text = message_to_text(last_assistant_msg)
    elif isinstance(content, str):
        text = content
    else:
        text = str(content or "")

    if not text or not text.strip():
        raise ValueError("Assistant message is empty")

    # Write to clipboard
    text_to_copy = text.strip()
    try:
        result_json = _tool_clipboard_write({"text": text_to_copy})
        result = json.loads(result_json)
        if result.get("ok"):
            method = result.get("method", "native")
            # Extract OSC52 sequence if present and store it in session
            # so it can be written to stdout later
            osc52_sequence = result.get("osc52_sequence")
            if isinstance(osc52_sequence, str) and not sess.pending_osc52_sequence:
                sess.pending_osc52_sequence = osc52_sequence
            return f"Copied to clipboard ({len(text_to_copy)} chars) via {method}"
        else:
            error_msg = result.get("error", "Unknown error")
            raise ValueError(error_msg)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse clipboard result: {e}") from e
    except Exception as e:
        raise ValueError(f"Failed to copy to clipboard: {e}") from e


# Simple command registry and help text
_COMMANDS_USAGE = {
    "/exit": "Exit the REPL",
    "/quit": "Exit the REPL (alias)",
    "/model <name>": "Override the current model",
    "/agent <name>": "Switch to a configured agent",
    "/reasoning_effort [minimal|low|medium|high]": "Set reasoning effort for current agent",
    "/tools": "List discovered MCP tools for current agent",
    "/no-tools [on|off]": "Toggle or set MCP tool usage for this session",
    "/info": "Show session/model info and usage",
    "/file <path>": "Attach a file to the conversation",
    "/copy": "Copy last assistant message to clipboard",
    "/help": "Show available commands",
    "/compact": "Summarize and compact conversation history",
}


def get_command_names() -> List[str]:
    return [
        "/exit",
        "/quit",
        "/model",
        "/agent",
        "/reasoning_effort",
        "/tools",
        "/no-tools",
        "/info",
        "/file",
        "/copy",
        "/help",
        "/compact",
    ]


def command_help() -> str:
    lines = ["Available commands:"]
    for cmd, desc in _COMMANDS_USAGE.items():
        lines.append(f"  {cmd:45} - {desc}")
    return "\n".join(lines)


def _is_continuation(text: str, multiline_mode: bool = False) -> bool:
    """Check if a line expects continuation (has unclosed brackets/parens/backticks).

    Args:
        text: The accumulated text so far
        multiline_mode: If True, never auto-continue (user controls via Ctrl+S)

    Returns True if:
    - Line ends with backslash (explicit continuation)
    - Has unclosed brackets/parens/braces
    - Has unclosed markdown code blocks (triple backticks)
    """
    # If in multiline mode, user controls submission via Ctrl+S
    if multiline_mode:
        return False

    stripped = text.rstrip()

    # 1. Check for trailing backslash (explicit continuation)
    if stripped.endswith("\\"):
        return True

    # 2. Count brackets to detect unclosed ones
    try:
        open_parens = stripped.count("(") - stripped.count(")")
        open_brackets = stripped.count("[") - stripped.count("]")
        open_braces = stripped.count("{") - stripped.count("}")

        if open_parens > 0 or open_brackets > 0 or open_braces > 0:
            return True
    except Exception:
        pass

    # 3. Markdown code blocks (triple backticks)
    # Odd count of ``` means we're inside a code block
    try:
        backtick_count = stripped.count("```")
        if backtick_count % 2 == 1:
            return True
    except Exception:
        pass

    return False


def setup_multiline_key_bindings() -> KeyBindings:
    """Setup key bindings for multiline mode.

    - Enter: insert newline (normal editing)
    - Ctrl+S: accept/submit input
    """
    bindings = KeyBindings()

    @bindings.add(Keys.ControlS)
    def _(event):
        """Ctrl+S - accept input and exit."""
        event.app.current_buffer.validate_and_handle()

    return bindings


async def _read_multiline_input(
    prompt_session: PromptSession,
    prompt_str: ANSI,
    continuation_prompt: str = "...> ",
    multiline_mode: bool = False,
) -> str:
    """Read input with support for multi-line modes.

    Two modes:
    1. Auto-continuation (multiline_mode=False, default):
       - When a line ends with backslash or has unclosed brackets/backticks,
         automatically prompt for continuation (shows "...> ")
       - This mimics Python's interactive mode

    2. Full multi-line (multiline_mode=True):
       - Enables true multi-line editing
       - Press Ctrl+S to submit
       - User controls when to submit

    Args:
        prompt_session: The PromptSession to use
        prompt_str: The main prompt to display
        continuation_prompt: Prompt for continuation lines (auto mode)
        multiline_mode: If True, use full multi-line mode with Ctrl+S
    """
    if multiline_mode:
        # Full multi-line mode: user presses Ctrl+S to submit
        line = await prompt_session.prompt_async(prompt_str)
        return line
    else:
        # Auto-continuation mode: detect and accumulate lines
        lines = []
        current_prompt = prompt_str

        while True:
            try:
                line = await prompt_session.prompt_async(current_prompt)
            except KeyboardInterrupt:
                raise
            except EOFError:
                if lines:
                    # Return what we have accumulated
                    break
                raise

            lines.append(line)

            # Check if we need continuation
            accumulated = "\n".join(lines)
            if not _is_continuation(accumulated, multiline_mode=False):
                break

            # Switch to continuation prompt
            current_prompt = continuation_prompt

        return "\n".join(lines)


class ReplCompleter(Completer):
    """Custom completer for REPL slash commands and arguments."""

    def __init__(self, get_agent_names: Callable[[], List[str]]):
        self.get_agent_names = get_agent_names
        self.commands = get_command_names()

    def get_completions(self, document: Document, complete_event) -> List[Completion]:
        """Return completions for current input."""
        text = document.text_before_cursor
        completions: List[Completion] = []

        # If input doesn't start with /, no completions
        if not text.startswith("/"):
            return completions

        # Handle command completion (e.g., "/mod" -> "/model")
        parts = text.strip().split(None, 1)
        if len(parts) == 1 and not text.endswith(" "):
            # Completing the command itself
            cmd_prefix = parts[0]
            for cmd in self.commands:
                if cmd.startswith(cmd_prefix):
                    completions.append(Completion(cmd, start_position=-(len(cmd_prefix))))

        elif len(parts) > 1:
            # Completing arguments
            cmd = parts[0]
            arg_text = parts[1] if len(parts) > 1 else ""

            if cmd == "/agent":
                # Complete agent names
                try:
                    agent_names = self.get_agent_names()
                    for name in agent_names:
                        if name.startswith(arg_text):
                            completions.append(Completion(name, start_position=-(len(arg_text))))
                except Exception:
                    pass

            elif cmd == "/reasoning_effort":
                # Complete reasoning effort levels
                for level in ["minimal", "low", "medium", "high"]:
                    if level.startswith(arg_text):
                        completions.append(Completion(level, start_position=-(len(arg_text))))

            elif cmd == "/file":
                # Complete file paths
                try:
                    if arg_text.startswith("~"):
                        arg_text = str(Path(arg_text).expanduser())

                    path = Path(arg_text)
                    if path.is_dir():
                        parent = path
                        prefix = ""
                    else:
                        parent = path.parent
                        prefix = path.name

                    if parent.exists():
                        for item in parent.iterdir():
                            if item.name.startswith(prefix):
                                suffix = "/" if item.is_dir() else ""
                                completions.append(
                                    Completion(
                                        item.name + suffix,
                                        start_position=-(len(prefix)),
                                    )
                                )
                except Exception:
                    pass

        return completions


async def run_agent_repl_async(
    *,
    agent: Any,
    config: Dict[str, Any],
    output_format: str,
    stream: bool,
    initial_user_message: Optional[Union[str, Dict[str, Any]]] = None,
    progress_reporter: Optional[Any] = None,
    session_ref: Optional[str] = None,
    sessions_enabled: Optional[bool] = None,
    copy: bool = False,
) -> None:
    """Interactive REPL loop using only a resolved Agent.

    - Displays a simple prompt "<agent>|<model>>".
    - On each turn executes the prompt with the agent (streaming or non-streaming).
    - Maintains a simple in-memory history for the current session.
    - Supports /help and /exit.
    """
    import time

    import click
    from rich.console import Console

    console = Console()
    console_err = Console(stderr=True)

    model = (getattr(agent.llm, "_base", {}) or {}).get("model")
    agent_label = getattr(agent, "name", "default") or "default"
    cli_model_override: Optional[str] = model

    # Extract prompt config options
    prompt_config = config.get("prompt", {})
    prompt_template = prompt_config.get("format")  # Optional: templized prompt format
    multiline_mode = prompt_config.get("multiline", False)
    show_hint = prompt_config.get("hint", True)  # Default: True (show help text)

    prompt_str = build_prompt(
        agent_name=agent_label,
        model=cli_model_override,
        template=prompt_template,
    )

    # Setup prompt_toolkit session
    history_file = Path.home() / ".gptsh_history"
    prompt_session = PromptSession(
        history=FileHistory(str(history_file)),
        completer=ReplCompleter(lambda: list((config.get("agents") or {}).keys())),
        multiline=multiline_mode,  # True for Ctrl+S mode, False for auto-detection
        mouse_support=False,
        key_bindings=setup_multiline_key_bindings() if multiline_mode else None,
    )

    # Ensure progress reporter is initialized for REPL turns
    try:
        if progress_reporter is not None:
            progress_reporter.start()
    except Exception:
        pass

    # Show help text for multiline mode on startup (if hint enabled)
    if multiline_mode and show_hint:
        try:
            from rich.console import Console as _Console

            _c = _Console(stderr=False)
            _c.print("[grey50]Press Ctrl+S to submit[/grey50]")
        except Exception:
            click.echo("(Press Ctrl+S to submit)", err=True)

    try:
        no_tools = not any(len(v or []) > 0 for v in (agent.tools or {}).values())
    except Exception as e:
        _log.debug("Failed to inspect agent tools: %s", e)
        no_tools = True

    # Session history lives in ChatSession; no external history list
    last_interrupt = 0.0
    try:
        from gptsh.mcp.manager import MCPManager as _MCPManager
    except Exception:  # pragma: no cover - fallback
        _MCPManager = None  # type: ignore
    mcp_manager = None if no_tools or _MCPManager is None else _MCPManager(config)

    # Preload session if requested
    preloaded_doc: Optional[Dict[str, Any]] = None
    if session_ref and (sessions_enabled is None or sessions_enabled):
        try:
            sid = _resolve_session_ref(str(session_ref))
            preloaded_doc = _load_session(sid)
        except Exception as e:
            click.echo(f"Warning: failed to load referenced session '{session_ref}': {e}", err=True)
            preloaded_doc = None
    # Ensure agent.session exists and can be preloaded
    if getattr(agent, "session", None) is None:
        from gptsh.core.session import ChatSession as _ChatSession

        try:
            sess = _ChatSession.from_agent(
                agent,
                progress=progress_reporter,
                config=config,
                mcp=(None if no_tools else mcp_manager),
            )
            await sess.start()
            agent.session = sess
        except Exception as e:
            _log.warning("Failed to initialize ChatSession for REPL preload: %s", e)
            console_err.print(f"[yellow]Warning: failed to initialize session: {e}[/yellow]")

    # Load instruction files from config if session exists
    sess = getattr(agent, "session", None)
    if sess is not None:
        try:
            from gptsh.core.utils import load_instruction_files, resolve_instructions

            agent_name = getattr(agent, "name", None) or (config.get("default_agent") or "default")
            instructions = resolve_instructions(config, agent_name)
            if instructions:
                instructions_content = await load_instruction_files(instructions)
                if instructions_content.strip():
                    sess.history.append(
                        {
                            "role": "user",
                            "content": instructions_content,
                            "_instruction": True,
                        }
                    )
                    _log.debug("Loaded %d instruction files into session", len(instructions))
        except Exception as e:
            _log.warning("Failed to load instruction files: %s", e)

    if (
        preloaded_doc
        and getattr(agent, "session", None) is not None
        and (sessions_enabled is None or sessions_enabled)
    ):
        from gptsh.core.sessions import preload_session_to_chat as _preload_chat

        _preload_chat(preloaded_doc, agent.session)

    async def _perform_auto_copy() -> None:
        """Auto-copy last assistant message if --copy flag is set."""
        if not copy:
            return
        try:
            copy_msg = command_copy(agent)
            sess = getattr(agent, "session", None)
            if progress_reporter is not None:
                async with progress_reporter.aio_io():
                    console_err.print(f"[grey50]{copy_msg}[/grey50]")
                    # Write OSC52 inside aio_io context to avoid buffering issues
                    if sess is not None:
                        await sess.write_pending_osc52()
            else:
                console_err.print(f"[grey50]{copy_msg}[/grey50]")
                if sess is not None:
                    await sess.write_pending_osc52()
        except Exception as e:
            _log.error("Auto-copy failed: %s", e)
            console_err.print(f"[red]Copy error: {e}[/red]")

    async def _run_once(
        user_message: Union[str, Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        nonlocal preloaded_doc
        # Use the persistence-aware runner so titles are generated and turns are saved consistently
        from gptsh.core.runner import (
            RunRequest as _RunRequest,
            run_turn_with_persistence as _run_persist,
        )

        # Ensure a ChatSession exists on the agent (runner can also create it if needed)
        sess = getattr(agent, "session", None)

        # Prepare or reuse a session document when sessions are enabled
        doc = preloaded_doc
        msgs: List[Dict[str, Any]] = []  # not used by persistence runner, kept for signature

        # Resolve small model for title generation
        small_model = _resolve_small_model(
            (config.get("agents") or {}).get(agent_label) or {},
            (config.get("providers") or {}).get(config.get("default_provider")) or {},
        ) or (getattr(agent.llm, "_base", {}) or {}).get("model")

        # Create a new session doc if needed and sessions are enabled
        from gptsh.core.sessions import new_session_doc as __new_session_doc

        if doc is None and (sessions_enabled is None or sessions_enabled):
            chosen_model = (getattr(agent.llm, "_base", {}) or {}).get("model")
            agent_info = {
                "name": getattr(agent, "name", None),
                "model": chosen_model,
                "model_small": small_model or chosen_model,
                "prompt_system": (
                    (
                        (config.get("agents", {}) or {}).get(getattr(agent, "name", "default"), {})
                        or {}
                    ).get("prompt", {})
                    or {}
                ).get("system"),
                "params": {
                    k: v
                    for k, v in (getattr(agent, "generation_params", {}) or {}).items()
                    if k in {"temperature", "reasoning_effort"} and v is not None
                },
            }
            provider_info = {"name": (config.get("default_provider") or None)}
            doc = __new_session_doc(
                agent_info=agent_info,
                provider_info=provider_info,
                output=output_format,
                mcp_allowed_servers=(config.get("mcp", {}) or {}).get("allowed_servers"),
            )

        req = _RunRequest(
            agent=agent,
            user_message=user_message,
            config=config,
            stream=stream,
            output_format=output_format,
            no_tools=no_tools,
            logger=console,
            exit_on_interrupt=False,
            result_sink=None,  # runner prints to console directly
            messages_sink=None,  # persistence runner manages messages internally
            mcp_manager=mcp_manager,
            progress_reporter=progress_reporter,
            session=sess,
            session_doc=doc,
            small_model=small_model,
        )
        await _run_persist(req)
        # Persist updated doc reference for subsequent turns (same object mutated)
        preloaded_doc = doc
        return ("", msgs)

    while True:
        line = None
        if initial_user_message:
            # Handle initial message (may be multimodal dict or string)
            if isinstance(initial_user_message, dict):
                # Check if the multimodal message has actual content
                text_content = initial_user_message.get("text") or ""
                has_text = bool(isinstance(text_content, str) and text_content.strip())
                has_attachments = bool(initial_user_message.get("attachments"))
                if has_text or has_attachments:
                    # Convert to proper message format before running
                    from gptsh.core.multimodal import build_user_message as _build_user_message

                    model = (getattr(agent.llm, "_base", {}) or {}).get("model", "gpt-4o")
                    user_msg = _build_user_message(
                        text=text_content,
                        attachments=initial_user_message.get("attachments"),
                        model=model,
                    )
                    await _run_once(user_msg)
                    # Auto-copy after initial message turn
                    await _perform_auto_copy()
                initial_user_message = None
                # If dict was empty, proceed to input prompt (don't set line, let it fall through)
                if not (has_text or has_attachments):
                    line = None
                else:
                    continue
            elif initial_user_message:
                # Plain string - use as line if not empty
                line = initial_user_message
                initial_user_message = None
            else:
                initial_user_message = None

        if line is None:
            try:
                async with progress_reporter.aio_io():
                    line = await _read_multiline_input(
                        prompt_session, prompt_str, multiline_mode=multiline_mode
                    )
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_interrupt <= 1.5:
                    click.echo("", err=True)
                    break
                last_interrupt = now
                async with progress_reporter.aio_io():
                    console_err.print("\n[grey50]Press Ctrl-C again to exit[/grey50]")
                continue
            except EOFError:
                async with progress_reporter.aio_io():
                    click.echo("", err=True)
                break

        sline = line.strip()
        if not sline:
            continue

        if sline.startswith("/"):
            parts = sline.split(None, 1)
            cmd = parts[0]
            arg = parts[1] if len(parts) == 2 else None
            if cmd in ("/exit", "/quit"):
                click.echo("", err=True)
                break
            if cmd == "/help":
                click.echo(command_help())
                continue
            if cmd == "/info":
                click.echo(command_info(agent))
                continue
            if cmd == "/model":
                try:
                    new_override, new_prompt = command_model(
                        arg,
                        agent=agent,
                        agent_name=agent_label,
                        template=prompt_template,
                    )
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                    continue
                cli_model_override = new_override
                prompt_str = new_prompt
                continue
            if cmd == "/reasoning_effort":
                try:
                    command_reasoning_effort(arg, agent)
                except ValueError as ve:
                    click.echo(str(ve), err=True)

                continue
            if cmd == "/agent":
                try:
                    loop = asyncio.get_running_loop()
                    agent_conf_out, prompt_out, agent_name_out, no_tools, _mgr = command_agent(
                        arg,
                        config=config,
                        agent_conf={},
                        agent_name=agent_label,
                        provider_conf={},
                        cli_model_override=cli_model_override,
                        no_tools=no_tools,
                        mgr=None,
                        loop=loop,
                        template=prompt_template,
                    )
                    from gptsh.core.config_resolver import build_agent as _build_agent

                    agent = await _build_agent(
                        config,
                        cli_agent=agent_name_out,
                        cli_provider=None,
                        cli_tools_filter=None,
                        cli_model_override=None,
                        cli_no_tools=no_tools,
                    )
                    agent_label = agent_name_out
                    model = getattr(agent.llm, "_base", {}).get("model")
                    cli_model_override = model
                    prompt_str = prompt_out
                    if _MCPManager is not None:
                        mcp_manager = None if no_tools else (_MCPManager(config))
                except Exception as e:
                    _log.warning("Failed to switch agent: %s", e)
                    click.echo(f"Failed to switch agent: {e}", err=True)
                continue
            if cmd == "/tools":
                try:
                    click.echo(command_tools(agent))
                except Exception as e:
                    _log.warning("Failed to list tools: %s", e)
                    click.echo(f"Failed to list tools: {e}", err=True)
                continue
            if cmd == "/no-tools":
                try:
                    desired = (arg or "").strip().lower()
                    if desired not in {"", "on", "off"}:
                        click.echo("Usage: /no-tools [on|off]", err=True)
                        continue
                    new_agent, _no, msg = command_no_tools(
                        desired,
                        config=config,
                        agent_name=agent_label,
                        cli_model_override=cli_model_override,
                        current_no_tools=no_tools,
                    )
                    agent = new_agent
                    no_tools = _no
                    click.echo(msg)
                    if _MCPManager is not None:
                        mcp_manager = None if no_tools else (_MCPManager(config))
                except Exception as e:
                    _log.warning("Failed to toggle tools: %s", e)
                    click.echo(f"Failed to toggle tools: {e}", err=True)
                continue
            if cmd == "/file":
                try:
                    file_result = await command_file(arg, agent, config)
                    # Ensure session exists (create lazily if needed)
                    sess = getattr(agent, "session", None)

                    # Append as a user message to the active session
                    if sess is not None:
                        # Handle multimodal content (images, PDFs, audio) and text
                        if isinstance(file_result, dict):
                            file_type = file_result.get("type")
                            if file_type in ("image", "pdf"):
                                # Image or PDF: build multimodal message
                                from gptsh.core.multimodal import build_user_message

                                model = (getattr(agent.llm, "_base", {}) or {}).get(
                                    "model", "gpt-4o"
                                )
                                attachment = {
                                    "type": file_type,
                                    "mime": file_result.get("mime"),
                                    "data": file_result.get("data"),
                                }
                                message = build_user_message(
                                    text=f"Here's the {file_type} file: {file_result.get('name')}",
                                    attachments=[attachment],
                                    model=model,
                                )
                                sess.history.append(message)
                            elif file_type == "audio":
                                # Audio attachment - build multimodal message
                                from gptsh.core.multimodal import build_user_message

                                model = (getattr(agent.llm, "_base", {}) or {}).get(
                                    "model", "gpt-4o"
                                )
                                message = build_user_message(
                                    text=file_result.get("content"),
                                    attachments=[file_result.get("attachment")],
                                    model=model,
                                )
                                sess.history.append(message)
                            else:
                                # Marker or other content
                                sess.history.append({"role": "user", "content": str(file_result)})
                        else:
                            # String content (text or marker)
                            sess.history.append({"role": "user", "content": str(file_result)})
                        click.echo(f"File attached: {arg}")
                    else:
                        click.echo("No active session", err=True)
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                except Exception as e:
                    _log.warning("Failed to attach file: %s", e)
                    click.echo(f"Failed to attach file: {e}", err=True)
                continue
            if cmd == "/copy":
                try:
                    msg = command_copy(agent)
                    click.echo(msg)
                    # Write any pending OSC52 sequence from clipboard operation
                    sess = getattr(agent, "session", None)
                    if sess is not None:
                        await sess.write_pending_osc52()
                except ValueError as ve:
                    click.echo(str(ve), err=True)
                except Exception as e:
                    _log.warning("Failed to copy to clipboard: %s", e)
                    click.echo(f"Failed to copy to clipboard: {e}", err=True)
                continue
            if cmd == "/compact":
                sess = getattr(agent, "session", None)
                if sess is None:
                    click.echo("No active session", err=True)
                    continue
                # Resolve small model
                small_model = _resolve_small_model(
                    (config.get("agents") or {}).get(agent_label) or {},
                    (config.get("providers") or {}).get(config.get("default_provider")) or {},
                ) or (getattr(agent.llm, "_base", {}) or {}).get("model")
                summary = await sess.generate_summary(small_model=small_model)
                if not summary:
                    click.echo("No summary generated.")
                    continue
                # Preserve original system prompt; insert summary as first USER message and wipe rest
                hist = list(sess.history or [])
                new_hist: List[Dict[str, Any]] = []
                if hist and (hist[0].get("role") == "system"):
                    new_hist.append(hist[0])
                new_hist.append(
                    {
                        "role": "user",
                        "content": f"Summarized version of previous conversation:\n{summary}",
                    }
                )
                sess.history = new_hist
                # Print summary in current REPL format
                if output_format == "markdown":
                    try:
                        from rich.console import Console
                        from rich.markdown import Markdown

                        Console().print(Markdown(summary))
                    except Exception:
                        click.echo(summary)
                else:
                    click.echo(summary)
                continue
            click.echo("Unknown command", err=True)
            continue

        current_task = asyncio.create_task(_run_once(sline))
        try:
            result_text, msgs = await current_task
            # Persist turn to session store when sessions are enabled
            from gptsh.core.config_api import get_sessions_enabled

            if (
                get_sessions_enabled(
                    config,
                    agent_conf=(config.get("agents") or {}).get(agent_label),
                    no_sessions_cli=False,
                )
                if sessions_enabled is None
                else sessions_enabled
            ):
                try:
                    session_doc = preloaded_doc  # may be None initially
                except NameError:
                    session_doc = None
                sess = getattr(agent, "session", None)
                if session_doc is None:
                    chosen_model = (getattr(agent.llm, "_base", {}) or {}).get("model")
                    agent_info = {
                        "name": getattr(agent, "name", None),
                        "model": chosen_model,
                        "model_small": _resolve_small_model({}, {}) or chosen_model,
                        "prompt_system": (
                            (
                                (config.get("agents", {}) or {}).get(
                                    getattr(agent, "name", "default"), {}
                                )
                                or {}
                            ).get("prompt", {})
                            or {}
                        ).get("system"),
                        "params": {
                            k: v
                            for k, v in (getattr(agent, "generation_params", {}) or {}).items()
                            if k in {"temperature", "reasoning_effort"} and v is not None
                        },
                    }
                    provider_info = {"name": (config.get("default_provider") or None)}
                    session_doc = _new_session_doc(
                        agent_info=agent_info,
                        provider_info=provider_info,
                        output=output_format,
                        mcp_allowed_servers=(config.get("mcp", {}) or {}).get("allowed_servers"),
                    )
                from gptsh.core.sessions import save_after_turn as _save_after

                _save_after(session_doc, sess, msgs)
                preloaded_doc = session_doc
            # Auto-copy last assistant message if --copy flag is set
            await _perform_auto_copy()
        except (KeyboardInterrupt, asyncio.CancelledError):
            current_task.cancel()
            try:
                await current_task
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            last_interrupt = time.monotonic()
            async with progress_reporter.aio_io():
                console_err.print("[grey50]Request cancelled[/grey50]")
            continue


def run_agent_repl(
    *,
    agent: Any,
    config: Dict[str, Any],
    output_format: str,
    stream: bool,
    initial_user_message: Optional[Union[str, Dict[str, Any]]] = None,
    progress_reporter: Optional[Any] = None,
    session_ref: Optional[str] = None,
    sessions_enabled: Optional[bool] = None,
    copy: bool = False,
) -> None:
    asyncio.run(
        run_agent_repl_async(
            agent=agent,
            config=config,
            output_format=output_format,
            stream=stream,
            initial_user_message=initial_user_message,
            progress_reporter=progress_reporter,
            session_ref=session_ref,
            sessions_enabled=sessions_enabled,
            copy=copy,
        )
    )
