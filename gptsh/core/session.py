from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from gptsh.interfaces import LLMClient, MCPClient, ApprovalPolicy, ProgressReporter
from gptsh.llm.tool_adapter import build_llm_tools, parse_tool_calls


class ChatSession:
    """High-level orchestrator for a single prompt turn with optional tool use."""

    def __init__(
        self,
        llm: LLMClient,
        mcp: Optional[MCPClient],
        approval: ApprovalPolicy,
        progress: Optional[ProgressReporter],
        config: Dict[str, Any],
    ) -> None:
        self._llm = llm
        self._mcp = mcp
        self._approval = approval
        self._progress = progress
        self._config = config

    async def start(self) -> None:
        if self._mcp is not None:
            await self._mcp.start()

    async def run(
        self,
        prompt: str,
        provider_conf: Dict[str, Any],
        agent_conf: Optional[Dict[str, Any]] = None,
        cli_model_override: Optional[str] = None,
        no_tools: bool = False,
        history_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        params, has_tools, _model = await self._prepare_params(
            prompt, provider_conf, agent_conf, cli_model_override, no_tools, history_messages
        )
        if not has_tools:
            # Simple one-shot
            resp = await self._llm.complete(params)
            try:
                return str((resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
            except Exception:
                return ""
        # Tool loop
        conversation: List[Dict[str, Any]] = list(params.get("messages") or [])
        while True:
            params["messages"] = conversation
            resp = await self._llm.complete(params)
            calls = parse_tool_calls(resp)
            if not calls:
                try:
                    return str((resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
                except Exception:
                    return ""

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
            conversation.append({"role": "assistant", "content": None, "tool_calls": assistant_tool_calls})

            for call in calls:
                fullname = call["name"]
                if "__" not in fullname:
                    continue
                server, toolname = fullname.split("__", 1)
                # Parse args
                raw_args = call.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except Exception:
                    args = {}

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
                    continue

                task_id = None
                if self._progress is not None:
                    try:
                        pretty = json.dumps(args, ensure_ascii=False)
                    except Exception:
                        pretty = str(args)
                    task_id = self._progress.add_task(f"⏳ {server}__{toolname} args={pretty}")
                try:
                    result = await self._call_tool(server, toolname, args)
                except Exception as e:  # pragma: no cover - defensive
                    result = f"Tool execution failed: {e}"
                finally:
                    if self._progress is not None:
                        try:
                            self._progress.complete_task(task_id, f"✔ {server}__{toolname}")
                        except Exception:
                            pass

                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": fullname,
                        "content": result,
                    }
                )

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
            # Merge MCP settings from global + provider + agent, then build tool specs
            merged_conf = {
                "mcp": {
                    **((self._config.get("mcp", {}) or {})),
                    **(provider_conf.get("mcp", {}) or {}),
                    **(((agent_conf or {}).get("mcp", {})) or {}),
                }
            }
            tools = await build_llm_tools(merged_conf)
            if not tools:
                tools = await build_llm_tools(self._config)
            if tools:
                params["tools"] = tools
                if "tool_choice" not in params:
                    params["tool_choice"] = "auto"
                has_tools = True

        params["drop_params"] = True
        return params, has_tools, chosen_model

    async def _call_tool(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        if self._mcp is None:
            raise RuntimeError("MCP not available")
        return await self._mcp.call_tool(server, tool, args)

    async def prepare_stream(
        self,
        prompt: str,
        provider_conf: Dict[str, Any],
        agent_conf: Optional[Dict[str, Any]],
        cli_model_override: Optional[str],
        history_messages: Optional[List[Dict[str, Any]]],
    ) -> tuple[Dict[str, Any], str]:
        # Reuse parameter preparation but with tools disabled for streaming
        params, _has_tools, chosen_model = await self._prepare_params(
            prompt,
            provider_conf,
            agent_conf,
            cli_model_override,
            no_tools=True,
            history_messages=history_messages,
        )
        return params, chosen_model

    async def stream_with_params(self, params: Dict[str, Any]):
        async for chunk in self._llm.stream(params):
            yield chunk
