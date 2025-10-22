from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

from rich.console import Console

from gptsh.core.agent import Agent
from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.interfaces import ApprovalPolicy, LLMClient, MCPClient, ProgressReporter
from gptsh.llm.chunk_utils import extract_text
from gptsh.llm.tool_adapter import build_llm_tools, parse_tool_calls

# Serialize interactive approval prompts across concurrent tool tasks
PROMPT_LOCK: asyncio.Lock = asyncio.Lock()


class ChatSession:
    """High-level orchestrator for a single prompt turn with optional tool use."""

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

    @staticmethod
    def _normalize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize a messages list for provider compatibility.

        - Coerce None content to empty strings for roles that require content.
        - Remove assistant messages with tool_calls that are not followed by matching tool outputs.
        """
        # Coerce None content
        norm: List[Dict[str, Any]] = []
        for m in messages:
            m2 = dict(m)
            if m2.get("content") is None:
                # Providers often reject null content on messages (including those with tool_calls)
                m2["content"] = ""
            norm.append(m2)

        # Remove incomplete assistant tool_calls (no following tool message with matching id)
        result: List[Dict[str, Any]] = []
        i = 0
        while i < len(norm):
            cur = norm[i]
            if cur.get("role") == "assistant" and cur.get("tool_calls"):
                # Collect expected tool_call_ids
                call_ids = [tc.get("id") for tc in cur.get("tool_calls") or [] if isinstance(tc, dict)]
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
                    # Incomplete tool_calls sequence; drop the assistant tool_calls message
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
        """Construct a ChatSession from an Agent instance, including its tool specs."""
        return cls(agent.llm, mcp, agent.policy, progress, config, tool_specs=getattr(agent, "tool_specs", None))

    async def start(self) -> None:
        if self._mcp is not None:
            await self._mcp.start()

    async def aclose(self) -> None:
        """Close resources held by the session (MCP, LLM) in a best-effort, idempotent way."""
        if self._closed:
            return
        self._closed = True
        # Do not stop the shared ProgressReporter here; REPL owns its lifecycle.
        # Close MCP first so background tasks shut down
        try:
            if self._mcp is not None:
                if hasattr(self._mcp, "aclose") and callable(self._mcp.aclose):
                    await self._mcp.aclose()  # type: ignore[no-any-return]
                elif hasattr(self._mcp, "stop") and callable(self._mcp.stop):
                    await self._mcp.stop()  # type: ignore[no-any-return]
        except Exception:
            # Do not raise during shutdown
            pass
        # Close LLM client if it supports async close
        try:
            if hasattr(self._llm, "aclose") and callable(self._llm.aclose):
                await self._llm.aclose()  # type: ignore[no-any-return]
        except Exception:
            pass

    async def __aenter__(self) -> "ChatSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _prepare_params(
        self,
        prompt: str,
        provider_conf: Dict[str, Any],
        agent_conf: Optional[Dict[str, Any]],
        cli_model_override: Optional[str],
        no_tools: bool,
        history_messages: Optional[List[Dict[str, Any]]],
    ) -> tuple[Dict[str, Any], bool, str]:
        # Base params from provider
        params: Dict[str, Any] = {k: v for k, v in dict(provider_conf).items() if k not in {"model", "name"}}
        chosen_model = (
            cli_model_override
            or (agent_conf or {}).get("model")
            or provider_conf.get("model")
            or "gpt-4o"
        )
        messages: List[Dict[str, Any]] = []
        system_prompt = (agent_conf or {}).get("prompt", {}).get("system")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            for m in history_messages:
                if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool", "system"}:
                    messages.append(m)
        messages.append({"role": "user", "content": prompt})

        params["model"] = chosen_model
        # Normalize messages for provider compatibility
        params["messages"] = self._normalize_messages(messages)

        # Agent params merge
        agent_params: Dict[str, Any] = {}
        if agent_conf:
            nested = agent_conf.get("params") or {}
            if isinstance(nested, dict):
                for k, v in nested.items():
                    if k not in {"model", "name", "prompt", "mcp", "provider"}:
                        agent_params[k] = v
            allowed_agent_keys = {
                "temperature",
                "top_p",
                "top_k",
                "max_tokens",
                "presence_penalty",
                "frequency_penalty",
                "stop",
                "seed",
                "response_format",
                "reasoning",
                "reasoning_effort",
                "tool_choice",
                "parallel_tool_calls",
            }
            for k in allowed_agent_keys:
                if k in agent_conf and agent_conf[k] is not None:
                    agent_params[k] = agent_conf[k]
        if agent_params:
            params.update(agent_params)

        has_tools = False
        if not no_tools:
            specs = self._tool_specs
            if not specs:
                # Fallback to dynamic discovery based on merged MCP config
                merged_conf = {
                    "mcp": {
                        **((self._config.get("mcp", {}) or {})),
                        **(provider_conf.get("mcp", {}) or {}),
                        **(((agent_conf or {}).get("mcp", {})) or {}),
                    }
                }
                specs = await build_llm_tools(merged_conf)
                if not specs:
                    specs = await build_llm_tools(self._config)
                if specs:
                    # Cache for the remainder of this session
                    self._tool_specs = specs
            if specs:
                params["tools"] = specs
                if "tool_choice" not in params:
                    params["tool_choice"] = "auto"
                # Enable parallel tool calls by default when supported; can be overridden via config
                params.setdefault("parallel_tool_calls", True)
                has_tools = True
        return params, has_tools, chosen_model

    async def _call_tool(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        if self._mcp is None:
            raise RuntimeError("MCP not available")
        return await self._mcp.call_tool(server, tool, args)

    async def stream_turn(
        self,
        prompt: str,
        provider_conf: Dict[str, Any],
        agent_conf: Optional[Dict[str, Any]] = None,
        cli_model_override: Optional[str] = None,
        no_tools: bool = False,
        history_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[str]:
        """Unified streaming entry: streams assistant output and handles tools.

        - Streams text chunks for assistant messages.
        - If tool calls are requested (indicated by streamed deltas),
        reconstructs calls from streamed deltas, executes them,
        and loops until final assistant text is produced, without non-stream fallbacks.
        """

        # Ensure background resources are started and later shut down
        await self.start()
        try:
            params, has_tools, _model = await self._prepare_params(
                prompt, provider_conf, agent_conf, cli_model_override, no_tools, history_messages
            )
            conversation: List[Dict[str, Any]] = list(params.get("messages") or [])
            # Capture turn-level deltas to propagate back into provided history_messages
            turn_deltas: List[Dict[str, Any]] = []

            console_log = Console(stderr=True)

            # Prepare progress: single task for the whole turn
            working_task_id: Optional[int] = None
            working_task_label = f"Waiting for {_model}"
            while True:
                # Ensure waiting task exists for each LLM request
                if self._progress and working_task_id is None:
                    working_task_id = self._progress.add_task(working_task_label)

                # Normalize message history before each request
                params["messages"] = self._normalize_messages(list(conversation))
                if has_tools and self._tool_specs:
                    params["tools"] = self._tool_specs
                    params.setdefault("tool_choice", "auto")

                # Stream this assistant turn
                full_text = ""
                async for chunk in self._llm.stream(params):
                    text = extract_text(chunk)
                    if text:
                        full_text += text
                        yield text

                # Complete the waiting task when finishing the turn
                if self._progress and working_task_id is not None:
                    try:
                        self._progress.remove_task(working_task_id)
                    finally:
                        working_task_id = None

                # After streaming, determine if a tool round is needed
                info: Dict[str, Any] = (
                    self._llm.get_last_stream_info()  # type: ignore[attr-defined]
                    if hasattr(self._llm, "get_last_stream_info")
                    else {}
                )
                # If tools are disabled, finalize immediately with streamed text
                if not has_tools:
                    if full_text.strip():
                        final_msg = {"role": "assistant", "content": full_text}
                        conversation.append(final_msg)
                        turn_deltas.append(final_msg)
                    if history_messages is not None:
                        history_messages.extend(turn_deltas)
                    return

                # Prefer concrete tool calls from the streamed deltas; optionally fallback to non-stream if absent
                calls: List[Dict[str, Any]] = []
                streamed_calls: List[Dict[str, Any]] = (
                    self._llm.get_last_stream_calls()  # type: ignore[attr-defined]
                    if hasattr(self._llm, "get_last_stream_calls")
                    else []
                )
                # Decide if we actually need a tool round.
                # Run tools if:
                #  - provider streamed tool deltas, or
                #  - provider streamed concrete calls, or
                #  - provider streamed no visible text (intent-only), which is common for some providers, or
                #  - finish_reason explicitly indicates tool_calls.
                finish_reason = info.get("finish_reason")
                finish_indicates_tools = (str(finish_reason).lower() == "tool_calls") if finish_reason else False
                saw_deltas = bool(info.get("saw_tool_delta"))
                intent_only = full_text.strip() == ""
                need_tool_round = has_tools and (saw_deltas or bool(streamed_calls) or intent_only or finish_indicates_tools)
                if not need_tool_round:
                    # No tool intent detected; finalize with streamed text
                    if full_text.strip():
                        final_msg = {"role": "assistant", "content": full_text}
                        conversation.append(final_msg)
                        turn_deltas.append(final_msg)
                    if history_messages is not None:
                        history_messages.extend(turn_deltas)
                    return

                # Ensure any lead-in assistant text is flushed to stdout before tool logs
                if isinstance(full_text, str) and full_text.strip():
                    # Runner's MarkdownBuffer flushes on blank lines; emit one to preserve ordering
                    yield "\n\n"

                if streamed_calls:
                    for c in streamed_calls:
                        name = c.get("name")
                        if not name:
                            continue
                        args_json = c.get("arguments") or "{}"
                        calls.append({"id": c.get("id"), "name": name, "arguments": args_json})
                else:
                    # Optional fallback: deltas were seen OR provider suppressed text (intent-only);
                    # ask non-stream for structured calls
                    try:
                        resp = await self._llm.complete(params)
                    except Exception:
                        resp = {}
                    calls = parse_tool_calls(resp)
                    if not calls:
                        final_text = extract_text(resp) or full_text
                        if final_text and final_text.strip():
                            # Emit any final text from fallback and persist
                            yield final_text
                            final_msg = {"role": "assistant", "content": final_text}
                            conversation.append(final_msg)
                            turn_deltas.append(final_msg)
                            if history_messages is not None:
                                history_messages.extend(turn_deltas)
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
                # Preserve any streamed assistant lead-in text alongside tool_calls for correct history
                assistant_stub = {
                    "role": "assistant",
                    "content": (full_text if isinstance(full_text, str) and full_text.strip() else None),
                    "tool_calls": assistant_tool_calls,
                }
                conversation.append(assistant_stub)
                turn_deltas.append(assistant_stub)

                # Phase 1: collect approvals serially to avoid progress + prompt interleaving
                approved_calls: List[Dict[str, Any]] = []
                denied_tool_msgs: List[Dict[str, Any]] = []
                logs_to_print: List[str] = []
                enriched: List[Dict[str, Any]] = []  # keep parsed fields for execution phase
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
                        tool_args_str if len(tool_args_str) <= _max_args_len else tool_args_str[: _max_args_len - 1] + "…"
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
                        # Buffer denial log; will print after all tasks finish
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

                    # Approved
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

                # Append immediately the denied tool messages so the next model turn sees them
                for tool_msg in denied_tool_msgs:
                    conversation.append(tool_msg)
                    turn_deltas.append(tool_msg)

                # Phase 2: execute approved tools concurrently and append results in order
                async def _exec_one_enriched(item: Dict[str, Any]) -> Dict[str, Any]:
                    call = item["call"]
                    server = item["server"]
                    toolname = item["toolname"]
                    args = item["args"]
                    tool_args_str = item["tool_args_str"]
                    display_args = item["display_args"]

                    # Debounced per-tool progress task via progress helper (after approvals)
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
                                handle, f"[green]✔[/green] {server}__{toolname} args={tool_args_str}"
                            )

                    # Buffer success log; will print after all tasks finish
                    return {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": call.get("name", ""),
                        "content": result,
                        "_log": f"[green]✔[/green] [grey50]Executed tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{display_args}[/dim][/grey50]",
                    }

                results = await asyncio.gather(*[_exec_one_enriched(e) for e in enriched])
                for tool_msg in results:
                    # Pop internal log and append after execution completes
                    log_line = tool_msg.pop("_log", None)
                    if isinstance(log_line, str):
                        logs_to_print.append(log_line)
                    conversation.append(tool_msg)
                    turn_deltas.append(tool_msg)

                # Print all buffered logs (denials + successes) together after tasks finish
                if logs_to_print:
                    if self._progress:
                        async with self._progress.aio_io():
                            for line in logs_to_print:
                                console_log.print(line)
                    else:
                        for line in logs_to_print:
                            console_log.print(line)
        finally:
            # Ensure background tasks are torn down to avoid pending task warnings
            await self.aclose()
