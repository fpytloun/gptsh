from typing import Any, Dict, List, Optional, AsyncIterator, Mapping
import sys
from gptsh.llm.tool_adapter import build_llm_tools, parse_tool_calls
from gptsh.mcp import execute_tool_async

async def prepare_completion_params(
    prompt: str,
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]],
    cli_model_override: Optional[str],
    config: Dict[str, Any],
    no_tools: bool,
) -> tuple[Dict[str, Any], bool, str]:
    """
    Build LiteLLM acompletion params: messages, model, and tools (if enabled).
    Returns: (params, has_tools, chosen_model)
    """
    # Base params from provider, excluding non-LiteLLM keys
    params: Dict[str, Any] = {
        k: v for k, v in dict(provider_conf).items() if k not in {"model", "name"}
    }

    # Determine model priority
    chosen_model = (
        cli_model_override
        or (agent_conf or {}).get("model")
        or provider_conf.get("model")
        or "gpt-4o"
    )

    # Messages: system then user
    messages: List[Dict[str, Any]] = []
    system_prompt = (agent_conf or {}).get("prompt", {}).get("system")
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    params["model"] = chosen_model
    params["messages"] = messages

    # Merge additional generation params from agent config.
    # Support both agent_conf['params'] dict and selected top-level keys.
    agent_params: Dict[str, Any] = {}
    if agent_conf:
        # From nested 'params' dictionary (preferred)
        nested = agent_conf.get("params") or {}
        if isinstance(nested, dict):
            for k, v in nested.items():
                if k not in {"model", "name", "prompt", "mcp", "provider"}:
                    agent_params[k] = v
        # Whitelist selected top-level keys commonly supported by providers
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
    # Apply into LiteLLM params (provider_conf already contributed earlier)
    if agent_params:
        params.update(agent_params)

    has_tools = False
    if not no_tools:
        # Merge MCP settings from global + provider + agent
        merged_conf = {
            "mcp": {
                **((config.get("mcp", {}) or {})),
                **(provider_conf.get("mcp", {}) or {}),
                **(((agent_conf or {}).get("mcp", {})) or {}),
            }
        }
        tools = await build_llm_tools(merged_conf)
        if not tools:
            # fallback to global config only
            tools = await build_llm_tools(config)
        if tools:
            params["tools"] = tools
            # Respect any explicit tool_choice from agent params; default to "auto"
            if "tool_choice" not in params:
                params["tool_choice"] = "auto"
            has_tools = True

    # Ensure LiteLLM drops unsupported provider params gracefully
    params["drop_params"] = True
    return params, has_tools, chosen_model

async def stream_completion(params: Dict[str, Any]) -> AsyncIterator[str]:
    """
    Yield text chunks from a streaming completion. UI/printing must be handled by the caller.
    """
    from litellm import acompletion

    stream_iter = await acompletion(stream=True, **params)

    async for chunk in stream_iter:
        text = _extract_text(chunk)
        if text:
            yield text

async def complete_simple(params: Dict[str, Any]) -> str:
    """
    Single, non-streaming completion without tool loop. Returns final assistant content string.
    """
    from litellm import acompletion

    resp = await acompletion(**params)
    try:
        return str((resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
    except Exception:
        return ""

async def complete_with_tools(params: Dict[str, Any], config: Dict[str, Any], approved_map: Dict[str, List[str]], pause_ui=None, resume_ui=None, set_status=None, wait_label: Optional[str] = None) -> str:
    """
    Tool execution loop using MCP until the model returns a final message with no tool_calls.
    Returns final assistant content string.

    Tools not in the auto-approved set will trigger an interactive approval prompt.
    """
    from litellm import acompletion

    # Copy starting conversation from params
    conversation: List[Dict[str, Any]] = list(params.get("messages") or [])
    max_iters = 5
    for _ in range(max_iters):
        params["messages"] = conversation
        resp = await acompletion(**params)
        calls = parse_tool_calls(resp)
        if not calls:
            try:
                return str((resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
            except Exception:
                return ""
        # Append assistant tool_calls message
        assistant_tool_calls: List[Dict[str, Any]] = []
        for c in calls:
            fullname_c = c["name"]
            argstr_c = c.get("arguments")
            if not isinstance(argstr_c, str):
                try:
                    import json as _json
                    argstr_c = _json.dumps(argstr_c or {})
                except Exception:
                    argstr_c = "{}"
            assistant_tool_calls.append({
                "id": c.get("id"),
                "type": "function",
                "function": {
                    "name": fullname_c,
                    "arguments": argstr_c,
                },
            })
        conversation.append({
            "role": "assistant",
            "content": None,
            "tool_calls": assistant_tool_calls,
        })
        # Execute each tool call and append results
        for call in calls:
            fullname = call["name"]
            # Split "server__tool"
            if "__" in fullname:
                server, toolname = fullname.split("__", 1)
            else:
                continue
            # Parse arguments
            args_str = call.get("arguments") or "{}"
            try:
                import json as _json
                args = _json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
            except Exception:
                args = {}

            # Approval check with normalization:
            # - accept wildcard '*' at server or global level
            # - accept tool names with '-' or '_' interchangeably
            # - accept fully-qualified 'server__tool' entries in approvals
            raw_server_approvals = list(approved_map.get(server, []))
            raw_global_approvals = list(approved_map.get("*", []))

            def _canon(n: str) -> str:
                try:
                    return str(n).lower().replace("-", "_").strip()
                except Exception:
                    return str(n)

            server_approvals = set(_canon(x) for x in raw_server_approvals)
            global_approvals = set(_canon(x) for x in raw_global_approvals)
            canon_tool = _canon(toolname)
            canon_full = _canon(f"{server}__{toolname}")

            is_approved = (
                "*" in raw_server_approvals
                or "*" in raw_global_approvals
                or canon_tool in server_approvals
                or canon_tool in global_approvals
                or canon_full in server_approvals
                or canon_full in global_approvals
            )

            if not is_approved:
                try:
                    from rich.prompt import Confirm
                except Exception:
                    Confirm = None  # type: ignore
                pretty_args = "{}"
                try:
                    pretty_args = _json.dumps(args, ensure_ascii=False)
                except Exception:
                    pass
                question = f"Allow tool {server}__{toolname} with args {pretty_args}?"
                allowed = False
                if Confirm is not None:
                    # Pause running progress UI so the prompt is visible
                    try:
                        if callable(pause_ui):
                            pause_ui()
                    except Exception:
                        pass
                    try:
                        allowed = bool(Confirm.ask(question, default=False))
                    except Exception:
                        allowed = False
                    finally:
                        # Resume progress UI after user interaction
                        try:
                            if callable(resume_ui):
                                resume_ui()
                        except Exception:
                            pass
                else:
                    # Fallback to deny if Rich is unavailable
                    allowed = False
                if not allowed:
                    result = f"Denied by user: {server}__{toolname}"
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": fullname,
                        "content": result,
                    })
                    continue

            # Update progress/status to reflect tool execution
            if callable(set_status):
                try:
                    set_status(f"Executing {server}__{toolname}")
                except Exception:
                    pass
            try:
                result = await execute_tool_async(server, toolname, args, config)
            except Exception as e:
                result = f"Tool execution failed: {e}"
            finally:
                if callable(set_status) and wait_label:
                    try:
                        set_status(wait_label)
                    except Exception:
                        pass
            # Append tool result
            conversation.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": fullname,
                "content": result,
            })
    # Max iterations reached
    return ""

def _extract_text(c: Any) -> str:
    """
    Robustly extract text from various provider stream chunk shapes.
    """
    # 0) Direct string/bytes
    if isinstance(c, (str, bytes)):
        return c.decode() if isinstance(c, bytes) else c
    # 1) Mapping-like (dict or implements get)
    if isinstance(c, Mapping) or hasattr(c, "get"):
        try:
            m = c  # type: ignore
            # OpenAI-like
            content = (
                (m.get("choices", [{}])[0].get("delta", {}) or {}).get("content")
            )
            if content:
                return str(content)
            # Some providers put partial text under delta.text
            delta = (m.get("choices", [{}])[0].get("delta", {}) or {})
            text_val = delta.get("text") if isinstance(delta, Mapping) else None
            if text_val:
                return str(text_val)
            # Fallbacks
            message = (m.get("choices", [{}])[0].get("message", {}) or {})
            content = message.get("content") if isinstance(message, Mapping) else None
            if content:
                return str(content)
            if m.get("content"):
                return str(m.get("content"))
            if m.get("text"):
                return str(m.get("text"))
        except Exception:
            pass
    # 2) Attribute-based objects (e.g., litellm structured events)
    try:
        choices = getattr(c, "choices", None)
        if choices:
            first = choices[0] if len(choices) > 0 else None
            if first is not None:
                delta = getattr(first, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        return str(content)
                    text_val = getattr(delta, "text", None)
                    if text_val:
                        return str(text_val)
        content_attr = getattr(c, "content", None)
        if content_attr:
            return str(content_attr)
        text_attr = getattr(c, "text", None)
        if text_attr:
            return str(text_attr)
    except Exception:
        pass
    return ""
