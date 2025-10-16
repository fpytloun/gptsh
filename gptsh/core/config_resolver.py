from __future__ import annotations

from typing import Any, Dict, List, Optional

from gptsh.core.agent import Agent
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.config_api import compute_tools_policy, select_agent_provider_dicts
from gptsh.llm.litellm_client import LiteLLMClient
from gptsh.llm.tool_adapter import build_llm_tools_from_handles


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

    # Prepare a transient config copy to inject per-agent MCP servers override, if any.
    # We'll use this both for tool resolution and to fetch MCP server instructions.
    eff_config: Dict[str, Any] = dict(config or {})
    mcp_cfg: Dict[str, Any] = dict((eff_config.get("mcp") or {}))
    # Agent-level inline servers override: agent_conf.mcp.servers
    try:
        agent_mcp = (agent_conf or {}).get("mcp") if isinstance(agent_conf, dict) else None
        if isinstance(agent_mcp, dict) and "servers" in agent_mcp:
            # Agent-level inline servers take precedence; tools list further filters them
            mcp_cfg["servers_override"] = agent_mcp.get("servers")
            # When agent overrides servers, do not use CLI-provided servers files
            mcp_cfg.pop("servers_files_cli", None)
            mcp_cfg.pop("servers_files", None)
            # Ensure global inline servers don't interfere with override resolution
            if "servers" in mcp_cfg:
                mcp_cfg.pop("servers", None)
    except Exception:
        pass
    # Respect allowed servers filter when provided
    mcp_cfg["allowed_servers"] = list(allowed or [])
    eff_config["mcp"] = mcp_cfg

    # Build LiteLLMClient with effective base params
    base_params: Dict[str, Any] = {}
    if provider_conf:
        # Take provider config first
        base_params.update(**provider_conf)

    # provider model is baseline; agent may override; CLI overrides final
    base_params["model"] = (
        cli_model_override
        or (agent_conf.get("model") if isinstance(agent_conf, dict) else None)
        or provider_conf.get("model")
    )

    # Merge LiteLLM params from agent_conf
    for param in ["temperature", "reasoning_effort"]:
        if isinstance(agent_conf, dict) and agent_conf.get(param):
            base_params[param] = agent_conf.get(param)
    base_params["drop_params"] = True
    llm = LiteLLMClient(base_params=base_params)

    # Resolve tools if enabled
    # Import resolver lazily so tests can monkeypatch it reliably
    if no_tools:
        tools = {}
        tool_specs = []
        eff_config["mcp"] = {}
    else:
        from gptsh.mcp.tools_resolver import resolve_tools as _resolve_tools
        tools = await _resolve_tools(eff_config, allowed_servers=allowed)
        tool_specs = build_llm_tools_from_handles(tools)

        # Always fetch MCP initialize().instructions (async) and append to the agent system prompt
        try:
            from gptsh.mcp.client import _discover_server_instructions_async as _discover_server_instructions_async
            instructions_map = await _discover_server_instructions_async(eff_config)
        except Exception:
            instructions_map = {}

        if instructions_map:
            # Build a single appended guidance block
            blocks: List[str] = []
            for srv, text in instructions_map.items():
                if not text:
                    continue
                blocks.append(f"[Server: {srv}]\n{text.strip()}")
            if blocks:
                mcp_guidance = "\n\n---\n\nMCP server instructions (apply only when using these servers' tools):\n\n" + "\n\n".join(blocks)
                # Append to existing system prompt or create it
                if isinstance(agent_conf, dict):
                    prompt_obj = dict(agent_conf.get("prompt") or {})
                    base_system = (prompt_obj.get("system") or "")
                    prompt_obj["system"] = (base_system + mcp_guidance) if base_system else mcp_guidance
                    # Write back into agent_conf
                    agent_conf = dict(agent_conf)
                    agent_conf["prompt"] = prompt_obj

    # Build approval policy (merge global + agent approvals)
    try:
        # Use the same effective config for approvals to reflect per-agent servers
        from gptsh.mcp import get_auto_approved_tools
        approved_map = get_auto_approved_tools(eff_config if not no_tools else config, agent_conf=agent_conf)
    except Exception:
        approved_map = {}
    policy = DefaultApprovalPolicy(approved_map)

    name = cli_agent or config.get("default_agent") or "default"
    return Agent(
        name=name,
        llm=llm,
        tools=tools,
        tool_specs=tool_specs,
        policy=policy,
        generation_params={},
        provider_conf=dict(provider_conf or {}),
        agent_conf=dict(agent_conf or {}),
    )
