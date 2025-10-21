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
        - If tool calls are requested (often indicated by streamed deltas),
          performs a non-streaming complete() to retrieve calls, executes them,
          and loops until final assistant text is produced.
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
                need_tool_round = has_tools and (
                    bool(info.get("saw_tool_delta")) or (full_text.strip() == "")
                )
                if not need_tool_round:
                    # No tools requested; finalize with streamed text
                    if full_text.strip():
                        final_msg = {"role": "assistant", "content": full_text}
                        conversation.append(final_msg)
                        turn_deltas.append(final_msg)
                    # Persist deltas into caller-provided history, if any
                    if history_messages is not None:
                        history_messages.extend(turn_deltas)
                    # Ensure any stray progress task is removed before exiting
                    if self._progress and working_task_id is not None:
                        try:
                            self._progress.remove_task(working_task_id)
                        finally:
                            working_task_id = None
                    return

                # Prefer concrete tool calls from the streamed deltas; fallback to non-stream if absent
                calls: List[Dict[str, Any]] = []
                streamed_calls: List[Dict[str, Any]] = (
                    self._llm.get_last_stream_calls()  # type: ignore[attr-defined]
                    if hasattr(self._llm, "get_last_stream_calls")
                    else []
                )
                if streamed_calls:
                    for c in streamed_calls:
                        name = c.get("name")
                        if not name:
                            continue
                        args_json = c.get("arguments") or "{}"
                        calls.append({"id": c.get("id"), "name": name, "arguments": args_json})
                else:
                    resp = await self._llm.complete(params)
                    calls = parse_tool_calls(resp)
                    if not calls:
                        # No calls parsed; treat streamed text as final
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
                assistant_stub = {"role": "assistant", "content": None, "tool_calls": assistant_tool_calls}
                conversation.append(assistant_stub)
                turn_deltas.append(assistant_stub)

                # Execute tools concurrently and append results in order
                async def _exec_one(call: Dict[str, Any]) -> Dict[str, Any]:
                    fullname = call.get("name", "")
                    if "__" not in fullname:
                        return {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": fullname,
                            "content": f"Invalid tool name: {fullname}",
                        }
                    server, toolname = fullname.split("__", 1)
                    raw_args = call.get("arguments") or "{}"
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)

                    tool_args_str = json.dumps(args, ensure_ascii=False, default=str)

                    allowed = self._approval.is_auto_allowed(server, toolname)
                    if not allowed:
                        # Pause progress and serialize approval prompts globally
                        if self._progress:
                            async with PROMPT_LOCK:
                                async with self._progress.aio_io():
                                    allowed = await self._approval.confirm(server, toolname, args)
                        else:
                            async with PROMPT_LOCK:
                                allowed = await self._approval.confirm(server, toolname, args)
                    if not allowed:
                        # Pause progress before console output
                        if self._progress:
                            async with self._progress.aio_io():
                                console_log.print(f"[yellow]⚠[/yellow] [grey50]Denied execution of tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args_str}[/dim][/grey50]")
                        else:
                            console_log.print(f"[yellow]⚠[/yellow] [grey50]Denied execution of tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args_str}[/dim][/grey50]")
                        if (self._config.get("mcp", {}) or {}).get("tool_choice") == "required":
                            raise ToolApprovalDenied(fullname)
                        return {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": fullname,
                            "content": f"Denied by user: {fullname}",
                        }

                    # Debounced per-tool progress task via progress helper
                    handle: Optional[int] = None
                    if self._progress:
                        handle = self._progress.start_debounced_task(
                            f"Executing tool {server}__{toolname} args={tool_args_str}",
                            delay=0.5
                        )

                    try:
                        result = await self._call_tool(server, toolname, args)
                    finally:
                        if self._progress and handle is not None:
                            self._progress.complete_debounced_task(
                                handle,
                                f"[green]✔[/green] {server}__{toolname} args={tool_args_str}",
                            )

                    # Pause progress before console output
                    if self._progress:
                        async with self._progress.aio_io():
                            console_log.print(f"[green]✔[/green] [grey50]Executed tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args_str}[/dim][/grey50]")
                    else:
                        console_log.print(f"[green]✔[/green] [grey50]Executed tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args_str}[/dim][/grey50]")
                    return {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": fullname,
                        "content": result,
                    }

                results = await asyncio.gather(*[_exec_one(c) for c in calls])
                for tool_msg in results:
                    conversation.append(tool_msg)
                    turn_deltas.append(tool_msg)
        finally:
            # Ensure background tasks are torn down to avoid pending task warnings
            await self.aclose()
