from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gptsh.core.session import ChatSession
from gptsh.llm.litellm_client import LiteLLMClient
from gptsh.mcp.manager import MCPManager
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.mcp.api import get_auto_approved_tools


async def run_prompt(
    *,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    no_tools: bool = False,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    progress_reporter=None,
) -> str:
    approved_map = get_auto_approved_tools(config, agent_conf=agent_conf)
    llm = LiteLLMClient()
    mcp_mgr = MCPManager(config) if not no_tools else None
    policy = DefaultApprovalPolicy(approved_map)
    session = ChatSession(llm, mcp_mgr, policy, progress_reporter, config)
    await session.start()
    return await session.run(
        prompt=prompt,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        no_tools=no_tools,
        history_messages=history_messages,
    )


async def prepare_stream_params(
    *,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    progress_reporter=None,
) -> Tuple[Dict[str, Any], str]:
    llm = LiteLLMClient()
    session = ChatSession(llm, None, DefaultApprovalPolicy({}), progress_reporter, config)
    return await session.prepare_stream(
        prompt=prompt,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        history_messages=history_messages,
    )

