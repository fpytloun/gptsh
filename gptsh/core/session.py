from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from rich.console import Console

from gptsh.core.agent import Agent
from gptsh.core.exceptions import ToolApprovalDenied
from gptsh.interfaces import ApprovalPolicy, LLMClient, MCPClient, ProgressReporter
from gptsh.llm.chunk_utils import extract_text
from gptsh.llm.tool_adapter import build_llm_tools, parse_tool_calls

logger = logging.getLogger(__name__)


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

    async def _prepare_params(
        self,
        prompt: str,
        provider_conf: Dict[str, Any],
        agent_conf: Optional[Dict[str, Any]],
        cli_model_override: Optional[str],
        no_tools: bool,
        history_messages: Optional[List[Dict[str, Any]]],
    ) -> tuple[Dict[str, Any], bool, str]:
        logger.debug(
            "Preparing params: no_tools=%s provider_keys=%s agent_keys=%s",
            no_tools,
            list((provider_conf or {}).keys()),
            list((agent_conf or {}).keys()) if agent_conf else [],
        )
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
        params["messages"] = messages

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
                params["tools"] = specs
                if "tool_choice" not in params:
                    params["tool_choice"] = "auto"
                has_tools = True
        logger.debug(
            "Prepared params: model=%s has_tools=%s tools_count=%d",
            chosen_model,
            has_tools,
            len(params.get("tools") or []),
        )

        params["drop_params"] = True
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


        params, has_tools, _model = await self._prepare_params(
            prompt, provider_conf, agent_conf, cli_model_override, no_tools, history_messages
        )
        conversation: List[Dict[str, Any]] = list(params.get("messages") or [])
        # Capture turn-level deltas to propagate back into provided history_messages
        turn_deltas: List[Dict[str, Any]] = []

        console_log = Console(stderr=True)

        # Prepare progress
        working_task_id: Optional[int] = None
        working_task_label = f"Waiting for {_model}"
        while True:
            params["messages"] = conversation
            if has_tools and self._tool_specs:
                params["tools"] = self._tool_specs
                params.setdefault("tool_choice", "auto")

            if self._progress:
                self._progress.start()
                working_task_id = self._progress.add_task(working_task_label)
            # Stream this assistant turn
            full_text = ""
            async for chunk in self._llm.stream(params):
                text = extract_text(chunk)
                if text:
                    full_text += text
                    yield text
            if self._progress:
                self._progress.complete_task(working_task_id)
                self._progress.stop()

            # After streaming, determine if a tool round is needed
            info: Dict[str, Any] = {}
            try:
                info = getattr(self._llm, "get_last_stream_info", lambda: {})()
            except Exception:
                info = {}
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
                    try:
                        history_messages.extend(turn_deltas)
                    except Exception:
                        pass
                return

            # Retrieve concrete tool calls via non-stream complete
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
                    try:
                        args_json = json.dumps(args_json or {})
                    except Exception:
                        args_json = "{}"
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

            # Execute tools and append results
            for call in calls:
                fullname = call["name"]
                if "__" not in fullname:
                    continue
                server, toolname = fullname.split("__", 1)
                raw_args = call.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except Exception:
                    args = {}

                tool_args = None
                try:
                    tool_args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    tool_args = str(args)

                allowed = self._approval.is_auto_allowed(server, toolname)
                if not allowed:
                    allowed = await self._approval.confirm(server, toolname, args)
                if not allowed:
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": fullname,
                            "content": f"Denied by user: {fullname}",
                        }
                    )
                    console_log.print(f"[yellow]⚠[/yellow] [grey50]Denied execution of tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args}[/dim][/grey50]")
                    if (self._config.get("mcp", {}) or {}).get("tool_choice") == "required":
                        raise ToolApprovalDenied(fullname)
                    continue

                task_id = None
                if self._progress is not None:
                    self._progress.start()
                    task_id = self._progress.add_task(f"Executing tool {server}__{toolname} args={tool_args}")
                try:
                    result = await self._call_tool(server, toolname, args)
                    tool_failed = False
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Tool execution error: %s: %s", fullname, e, exc_info=True)
                    result = f"Tool execution failed: {e}"
                    tool_failed = True
                finally:
                    if self._progress is not None and task_id is not None:
                        try:
                            self._progress.complete_task(task_id, f"{'[red]✖[/red]' if tool_failed else '[green]✔[/green]'} {server}__{toolname} args={tool_args}")
                            self._progress.stop()
                        except Exception:
                            pass
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": fullname,
                    "content": result,
                }
                conversation.append(tool_msg)
                turn_deltas.append(tool_msg)
                console_log.print(f"{'[red]✖[/red]' if tool_failed else '[green]✔[/green]'} [grey50]Executed tool [dim yellow]{server}__{toolname}[/dim yellow] with args [dim]{tool_args}[/dim][/grey50]")
