from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gptsh.core.agent import Agent
from gptsh.core.session import ChatSession
from gptsh.mcp.manager import MCPManager

"""
Agent-only core API.
Existing legacy helpers were removed in favor of Agent-based flow.
"""


async def run_prompt_with_agent(
    *,
    agent: Agent,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    no_tools: bool = False,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    progress_reporter=None,
) -> str:
    mcp_mgr = MCPManager(config) if not no_tools else None
    session = ChatSession.from_agent(agent, mcp=mcp_mgr, progress=progress_reporter, config=config)
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
    agent: Agent,
    prompt: str,
    config: Dict[str, Any],
    provider_conf: Dict[str, Any],
    agent_conf: Optional[Dict[str, Any]] = None,
    cli_model_override: Optional[str] = None,
    history_messages: Optional[List[Dict[str, Any]]] = None,
    progress_reporter=None,
) -> Tuple[Dict[str, Any], str]:
    session = ChatSession.from_agent(agent, mcp=None, progress=progress_reporter, config=config)
    return await session.prepare_stream(
        prompt=prompt,
        provider_conf=provider_conf,
        agent_conf=agent_conf,
        cli_model_override=cli_model_override,
        history_messages=history_messages,
    )
