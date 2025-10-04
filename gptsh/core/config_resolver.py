from __future__ import annotations

from typing import Any, Dict, List, Optional

from gptsh.core.agent import Agent
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.config_api import compute_tools_policy, select_agent_provider_dicts
from gptsh.llm.litellm_client import LiteLLMClient


async def build_agent(
    config: Dict[str, Any],
    *,
    cli_agent: Optional[str] = None,
    cli_provider: Optional[str] = None,
    cli_tools_filter: Optional[List[str]] = None,
    cli_model_override: Optional[str] = None,
    cli_no_tools: bool = False,
) -> Agent:
    agent_conf, provider_conf = select_agent_provider_dicts(config, cli_agent=cli_agent, cli_provider=cli_provider)

    # Compute allowed servers and no_tools based on agent + CLI
    no_tools, allowed = compute_tools_policy(agent_conf, cli_tools_filter, cli_no_tools)

    # Build LiteLLMClient with effective base params
    base_params: Dict[str, Any] = {}
    # provider model is baseline; agent may override; CLI overrides final
    base_params["model"] = (
        cli_model_override
        or (agent_conf.get("model") if isinstance(agent_conf, dict) else None)
        or provider_conf.get("model")
    )
    # Merge generation params from agent_conf["params"] if present
    if isinstance(agent_conf, dict) and isinstance(agent_conf.get("params"), dict):
        for k, v in agent_conf["params"].items():
            base_params[k] = v
    base_params["drop_params"] = True
    llm = LiteLLMClient(base_params=base_params)

    # Resolve tools if enabled
    # Import resolver lazily so tests can monkeypatch it reliably
    if no_tools:
        tools = {}
    else:
        from gptsh.mcp.tools_resolver import resolve_tools as _resolve_tools
        tools = await _resolve_tools(config, allowed_servers=allowed)

    # Build approval policy (merge global + agent approvals)
    try:
        from gptsh.mcp import get_auto_approved_tools
        approved_map = get_auto_approved_tools(config, agent_conf=agent_conf)
    except Exception:
        approved_map = {}
    policy = DefaultApprovalPolicy(approved_map)

    name = cli_agent or config.get("default_agent") or "default"
    return Agent(name=name, llm=llm, tools=tools, policy=policy, generation_params={})
