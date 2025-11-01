from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from litellm.types.utils import Usage
from rich.console import Console

from gptsh.core.agent import Agent
from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.interfaces import ApprovalPolicy, LLMClient, MCPClient, ProgressReporter
from gptsh.llm.chunk_utils import extract_text
from gptsh.llm.tool_adapter import build_llm_tools, parse_tool_calls

_log = logging.getLogger(__name__)

# Serialize interactive approval prompts across concurrent tool tasks
PROMPT_LOCK: asyncio.Lock = asyncio.Lock()


class ChatSession:
    """High-level orchestrator for chat turns with optional tool use.

    New minimal contract: build request params from the LLM base (llm._base)
    plus provided messages/history. No per-turn provider/agent config merges.
    """

    def __init__(
        self,
        llm: LLMClient,
        mcp: Optional[MCPClient],
        approval: ApprovalPolicy,
        progress: Optional[ProgressReporter],
        config: Dict[str, Any],
        *,
        tool_specs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._llm = llm
        self._mcp = mcp
        self._approval = approval
        self._progress = progress
        self._config = config
        self._tool_specs: List[Dict[str, Any]] = list(tool_specs or [])
        self._closed: bool = False
        self.usage: Dict[str, Any] = {}
        # In-session message history managed by ChatSession
        self.history: List[Dict[str, Any]] = []
        # Optional title for the conversation
        self.title: Optional[str] = None

    async def ensure_title(self, small_model: Optional[str]) -> Optional[str]:
        """Generate and set a short title for this session if not set yet.

        Uses the first user message in the current history and a configured
        small model if available. Returns the title or None if unavailable.
        """
        try:
            if self.title:
                return self.title
            if not isinstance(small_model, str) or not small_model.strip():
                return None
            # Find first user message content
            first_user: Optional[str] = None
            first_assistant: Optional[str] = None

            # Lazy import to avoid circulars
            from gptsh.core.multimodal import message_to_text as _msg_to_text  # noqa: WPS433

            for m in self.history:
                if not first_user and m.get("role") == "user":
                    content = m.get("content")
                    if isinstance(content, list):
                        # Content array - extract text parts only
                        text = _msg_to_text(m)
                    elif isinstance(content, str):
                        text = content.strip()
                    else:
                        text = str(content or "").strip()
                    if text:
                        first_user = text

                if not first_assistant and m.get("role") == "assistant":
                    content = m.get("content")
                    if isinstance(content, list):
                        # Content array - extract text parts only
                        text = _msg_to_text(m)
                    elif isinstance(content, str):
                        text = content.strip()
                    else:
                        text = str(content or "").strip()
                    if text:
                        first_assistant = text

                if first_user and first_assistant:
                    break

            if not first_user and not first_assistant:
                return None

            from gptsh.core.sessions import generate_title as _gen_title  # noqa: WPS433

            # Build concise conversation string for title generation
            conversation_parts = []
            if first_user:
                conversation_parts.append(f"USER: {first_user}")
            if first_assistant:
                conversation_parts.append(f"ASSISTANT: {first_assistant}")

            title = await _gen_title(
                "\n".join(conversation_parts),
                small_model=small_model,
                llm=self._llm,
            )
            if isinstance(title, str) and title.strip():
                self.title = title.strip()
                return self.title
            return None
        except Exception:
            return None

    def _update_usage(self, usage: Usage) -> None:
        self.usage = {
            "tokens": {
                "completion": getattr(usage, "completion_tokens", None),
                "prompt": getattr(usage, "prompt_tokens", None),
                "total": getattr(usage, "total_tokens", None),
                "reasoning_tokens": getattr(
                    getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
                ),
                "cached_tokens": getattr(
                    getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
                ),
            },
            "cost": self.usage.get("cost", 0),
        }
        if getattr(usage, "cost", None):
            self.usage["cost"] += usage.cost  # type: ignore[operator]

    @staticmethod
    def _normalize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Coerce None content and drop incomplete tool_call sequences
        norm: List[Dict[str, Any]] = []
        for m in messages:
            m2 = dict(m)
            if m2.get("content") is None:
                m2["content"] = ""
            norm.append(m2)
        result: List[Dict[str, Any]] = []
        i = 0
        while i < len(norm):
            cur = norm[i]
            if cur.get("role") == "assistant" and cur.get("tool_calls"):
                call_ids = [
                    tc.get("id") for tc in cur.get("tool_calls") or [] if isinstance(tc, dict)
                ]
                j = i + 1
                seen_ids = set()
                while j < len(norm):
                    nxt = norm[j]
                    if nxt.get("role") != "tool":
                        break
                    tcid = nxt.get("tool_call_id")
                    if tcid:
                        seen_ids.add(tcid)
                    j += 1
                if call_ids and not set(call_ids).issubset(seen_ids):
                    i += 1
                    continue
            result.append(cur)
            i += 1
        return result

    @classmethod
    def from_agent(
        cls,
        agent: Agent,
        *,
        progress: Optional[ProgressReporter],
        config: Dict[str, Any],
        mcp: Optional[MCPClient] = None,
    ) -> "ChatSession":
        session = cls(
            agent.llm,
            mcp,
            agent.policy,
            progress,
            config,
            tool_specs=getattr(agent, "tool_specs", None),
        )
        # Seed system prompt from config into empty/new histories
        try:
            agent_name = getattr(agent, "name", None) or (config.get("default_agent") or "default")
            sys_prompt = (
                ((config.get("agents") or {}).get(agent_name) or {}).get("prompt", {})
            ).get("system")
            if isinstance(sys_prompt, str) and sys_prompt.strip():
                hist = getattr(session, "history", []) or []
                if not hist or (hist and (hist[0].get("role") != "system")):
                    session.history = [{"role": "system", "content": sys_prompt}] + list(hist)
        except Exception:
            # Best-effort; absence of system prompt should not break session creation
            pass
        return session

    async def start(self) -> None:
        if self._mcp is not None:
            await self._mcp.start()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._mcp is not None:
                if hasattr(self._mcp, "aclose") and callable(self._mcp.aclose):
                    await self._mcp.aclose()  # type: ignore[misc]
                elif hasattr(self._mcp, "stop") and callable(self._mcp.stop):
                    await self._mcp.stop()  # type: ignore[misc]
        except Exception:
            pass
        try:
            if hasattr(self._llm, "aclose") and callable(self._llm.aclose):
                await self._llm.aclose()  # type: ignore[misc]
        except Exception:
            pass

    async def __aenter__(self) -> "ChatSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def generate_summary(self, *, small_model: Optional[str]) -> Optional[str]:
        """Summarize current session history using a smaller model.

        Returns summary text or None on failure.
        """
        try:
            if not isinstance(small_model, str) or not small_model.strip():
                return None
            # Build transcript from history, ignoring tool messages
            lines: List[str] = []
            for m in self.history:
                role = m.get("role")
                if role == "user":
                    text = str(m.get("content") or "").strip()
                    if text:
                        lines.append(f"USER: {text}")
                elif role == "assistant":
                    text = str(m.get("content") or "").strip()
                    if text:
                        lines.append(f"ASSISTANT: {text}")
            convo = "\n".join(lines)
            if not convo.strip():
                return None
            system = (
                "You compress the provided conversation into a concise, faithful summary "
                "retaining key decisions, constraints, and next steps. No extra commentary."
            )
            params: Dict[str, Any] = {
                "model": small_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": convo},
                ],
                "temperature": 0.2,
            }
            resp = await self._llm.complete(params)
            from gptsh.llm.chunk_utils import extract_text as _extract  # lazy import

            text = str(_extract(resp) or "").strip()
            return text or None
        except Exception:
            return None

    async def _prepare_params(
        self,
        user_message: Union[str, Dict[str, Any]],
        no_tools: bool,
    ) -> tuple[Dict[str, Any], bool, str]:
        # Build params from LLM base only
        params: Dict[str, Any] = {}
        base = getattr(self._llm, "_base", {}) or {}
        chosen_model = base.get("model") or "gpt-4o"

        messages: List[Dict[str, Any]] = []
        for m in self.history:
            if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool", "system"}:
                messages.append(m)

        # Accept either a string (convert to message) or a full message dict
        if isinstance(user_message, str):
            messages.append({"role": "user", "content": user_message})
        elif isinstance(user_message, dict):
            messages.append(user_message)
        else:
            raise TypeError(f"user_message must be str or dict, got {type(user_message)}")

        params["model"] = chosen_model
        params["messages"] = self._normalize_messages(messages)

        has_tools = False
        if not no_tools:
            specs = self._tool_specs
            if not specs:
                specs = await build_llm_tools(self._config)
                if specs:
                    self._tool_specs = specs
            if specs:
                params["tools"] = specs
                params.setdefault("tool_choice", "auto")
                params.setdefault("parallel_tool_calls", True)
                has_tools = True
        return params, has_tools, chosen_model

    async def _call_tool(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        if self._mcp is None:
            raise RuntimeError("MCP not available")
        return await self._mcp.call_tool(server, tool, args)

    async def stream_turn(
        self,
        user_message: Union[str, Dict[str, Any]],
        no_tools: bool = False,
    ) -> AsyncIterator[str]:
        # Per-turn progress task
        working_task_id: Optional[int] = None
        try:
            params, has_tools, _model = await self._prepare_params(user_message, no_tools)
            # Initialize conversation from params messages
            conversation: List[Dict[str, Any]] = list(params.get("messages") or [])
            turn_deltas: List[Dict[str, Any]] = []

            # Convert user_message to proper message dict for history
            if isinstance(user_message, str):
                user_msg_for_history = {"role": "user", "content": user_message}
            else:
                user_msg_for_history = user_message

            console_log = Console(stderr=True)
            working_task_label = f"Waiting for {_model}"
            while True:
                if self._progress and working_task_id is None:
                    working_task_id = self._progress.add_task(working_task_label)

                params["messages"] = self._normalize_messages(list(conversation))
                if has_tools and self._tool_specs:
                    params["tools"] = self._tool_specs
                    params.setdefault("tool_choice", "auto")

                full_text = ""
                async for chunk in self._llm.stream(params):
                    if getattr(chunk, "usage", None):
                        self._update_usage(chunk.usage)  # type: ignore[attr-defined]
                    text = extract_text(chunk)
                    if text:
                        full_text += text
                        yield text

                if self._progress and working_task_id is not None:
                    try:
                        self._progress.remove_task(working_task_id)
                    finally:
                        working_task_id = None

                info: Dict[str, Any] = (
                    self._llm.get_last_stream_info()  # type: ignore[attr-defined]
                    if hasattr(self._llm, "get_last_stream_info")
                    else {}
                )
                if not has_tools:
                    if full_text.strip():
                        final_msg = {"role": "assistant", "content": full_text}
                        conversation.append(final_msg)
                        turn_deltas.append(final_msg)
                    # Persist deltas and current user to session history
                    self.history.append(user_msg_for_history)
                    if turn_deltas:
                        self.history.extend(turn_deltas)
                    return

                calls: List[Dict[str, Any]] = []
                streamed_calls: List[Dict[str, Any]] = (
                    self._llm.get_last_stream_calls()  # type: ignore[attr-defined]
                    if hasattr(self._llm, "get_last_stream_calls")
                    else []
                )
                finish_reason = info.get("finish_reason")
                finish_indicates_tools = (
                    (str(finish_reason).lower() == "tool_calls") if finish_reason else False
                )
                saw_deltas = bool(info.get("saw_tool_delta"))
                intent_only = full_text.strip() == ""
                need_tool_round = has_tools and (
                    saw_deltas or bool(streamed_calls) or intent_only or finish_indicates_tools
                )
                if not need_tool_round:
                    if full_text.strip():
                        final_msg = {"role": "assistant", "content": full_text}
                        conversation.append(final_msg)
                        turn_deltas.append(final_msg)
                    self.history.append(user_msg_for_history)
                    if turn_deltas:
                        self.history.extend(turn_deltas)
                    return

                if isinstance(full_text, str) and full_text.strip():
                    yield "\n\n"

                if streamed_calls:
                    for c in streamed_calls:
                        name = c.get("name")
                        if not name:
                            continue
                        args_json = c.get("arguments") or "{}"
                        calls.append({"id": c.get("id"), "name": name, "arguments": args_json})
                else:
                    _log.debug(
                        "Non-stream fallback activated: saw_deltas=%s intent_only=%s finish_reason=%s",
                        saw_deltas,
                        intent_only,
                        finish_reason,
                    )
                    try:
                        resp = await self._llm.complete(params)
                    except Exception:
                        resp = {}
                    try:
                        usage_obj = getattr(resp, "usage", None)
                        if usage_obj is not None:
                            self._update_usage(usage_obj)
                    except Exception:
                        pass
                    calls = parse_tool_calls(resp)
                    if not calls:
                        final_text = extract_text(resp) or full_text
                        if final_text and final_text.strip():
                            yield final_text
                            final_msg = {"role": "assistant", "content": final_text}
                            conversation.append(final_msg)
                            turn_deltas.append(final_msg)
                        self.history.append(user_msg_for_history)
                        if turn_deltas:
                            self.history.extend(turn_deltas)
                        return

                assistant_tool_calls: List[Dict[str, Any]] = []
                for c in calls:
                    fn = c["name"]
                    args_json = c.get("arguments")
                    if not isinstance(args_json, str):
                        args_json = json.dumps(args_json or {}, default=str)
                    assistant_tool_calls.append(
                        {
                            "id": c.get("id"),
                            "type": "function",
                            "function": {"name": fn, "arguments": args_json},
                        }
                    )
                assistant_stub = {
                    "role": "assistant",
                    "content": (
                        full_text if isinstance(full_text, str) and full_text.strip() else None
                    ),
                    "tool_calls": assistant_tool_calls,
                }
                conversation.append(assistant_stub)
                turn_deltas.append(assistant_stub)

                approved_calls: List[Dict[str, Any]] = []
                denied_tool_msgs: List[Dict[str, Any]] = []
                logs_to_print: List[str] = []
                enriched: List[Dict[str, Any]] = []
                for call in calls:
                    fullname = call.get("name", "")
                    if "__" not in fullname:
                        denied_tool_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": fullname,
                                "content": f"Invalid tool name: {fullname}",
                            }
                        )
                        continue
                    server, toolname = fullname.split("__", 1)
                    raw_args = call.get("arguments") or "{}"
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    tool_args_str = json.dumps(args, ensure_ascii=False, default=str)
                    _max_args_len = 500
                    display_args = (
                        tool_args_str
                        if len(tool_args_str) <= _max_args_len
                        else tool_args_str[: _max_args_len - 1] + "…"
                    )

                    allowed = self._approval.is_auto_allowed(server, toolname)
                    if not allowed:
                        if self._progress:
                            async with PROMPT_LOCK:
                                async with self._progress.aio_io():
                                    allowed = await self._approval.confirm(server, toolname, args)
                        else:
                            async with PROMPT_LOCK:
                                allowed = await self._approval.confirm(server, toolname, args)

                    if not allowed:
                        logs_to_print.append(
                            f"[yellow]⚠[/yellow] [grey50]Denied execution of tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{display_args}[/dim][/grey50]"
                        )
                        if (self._config.get("mcp", {}) or {}).get("tool_choice") == "required":
                            raise ToolApprovalDenied(fullname)
                        denied_tool_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": fullname,
                                "content": f"Denied by user: {fullname}",
                            }
                        )
                        continue

                    approved_calls.append(call)
                    enriched.append(
                        {
                            "call": call,
                            "server": server,
                            "toolname": toolname,
                            "args": args,
                            "tool_args_str": tool_args_str,
                            "display_args": display_args,
                        }
                    )

                for tool_msg in denied_tool_msgs:
                    conversation.append(tool_msg)
                    turn_deltas.append(tool_msg)

                async def _exec_one_enriched(item: Dict[str, Any]) -> Dict[str, Any]:
                    call = item["call"]
                    server = item["server"]
                    toolname = item["toolname"]
                    args = item["args"]
                    tool_args_str = item["tool_args_str"]
                    display_args = item["display_args"]

                    handle: Optional[int] = None
                    if self._progress:
                        handle = self._progress.start_debounced_task(
                            f"Executing tool {server}__{toolname} args={tool_args_str}", delay=0.5
                        )
                    try:
                        result = await self._call_tool(server, toolname, args)
                    finally:
                        if self._progress and handle is not None:
                            self._progress.complete_debounced_task(
                                handle,
                                f"[green]✔[/green] {server}__{toolname} args={tool_args_str}",
                            )

                    return {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": call.get("name", ""),
                        "content": result,
                        "_log": f"[green]✔[/green] [grey50]Executed tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{display_args}[/dim][/grey50]",
                    }

                results = await asyncio.gather(*[_exec_one_enriched(e) for e in enriched])
                for tool_msg in results:
                    log_line = tool_msg.pop("_log", None)
                    if isinstance(log_line, str):
                        logs_to_print.append(log_line)
                    conversation.append(tool_msg)
                    turn_deltas.append(tool_msg)

                if logs_to_print:
                    if self._progress:
                        async with self._progress.aio_io():
                            for line in logs_to_print:
                                console_log.print(line)
                    else:
                        for line in logs_to_print:
                            console_log.print(line)
        finally:
            try:
                if self._progress and working_task_id is not None:
                    self._progress.remove_task(working_task_id)
            except Exception:
                pass
