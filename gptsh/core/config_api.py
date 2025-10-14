from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gptsh.core.models import map_config_to_models, pick_effective_agent_provider


def select_agent_provider_dicts(
    config: Dict[str, Any],
    cli_agent: Optional[str] = None,
    cli_provider: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    defaults, providers, agents = map_config_to_models(config)
    agent_dm, provider_dm = pick_effective_agent_provider(
        defaults, providers, agents, cli_agent=cli_agent, cli_provider=cli_provider
    )
    provider_conf: Dict[str, Any] = {"model": provider_dm.model, **(provider_dm.params or {})}
    agent_conf: Dict[str, Any] = {
        "provider": agent_dm.provider,
        "model": agent_dm.model,
        "prompt": {"system": agent_dm.prompt.system, "user": agent_dm.prompt.user},
        "mcp": agent_dm.mcp,
        "temperature": agent_dm.temperature,
        "reasoning_effort": agent_dm.reasoning_effort,
        "tools": agent_dm.tools,
        "no_tools": agent_dm.no_tools,
        "output": agent_dm.output,
    }
    return agent_conf, provider_conf


def effective_output(output_cli: Optional[str], agent_conf: Optional[Dict[str, Any]]) -> str:
    allowed = {"text", "markdown"}
    if output_cli in allowed:
        # If not allowed value (eg. default, agent config has precedence)
        return output_cli  # type: ignore[return-value]
    aout = (agent_conf or {}).get("output") if isinstance(agent_conf, dict) else None
    if aout in allowed:
        return str(aout)
    # Default or unknown resolves as markdown
    return "markdown"


def compute_tools_policy(
    agent_conf: Optional[Dict[str, Any]],
    tools_filter_cli: Optional[List[str]],
    no_tools_cli: bool,
) -> Tuple[bool, Optional[List[str]]]:
    """
    Determine effective no_tools flag and allowed server labels.
    Precedence: CLI no-tools overrides everything; else CLI tools filter; else agent.tools; else None.
    Returning allowed=None means "no restriction"; empty list means explicitly disabled.
    """
    if no_tools_cli:
        return True, []
    if tools_filter_cli is not None:
        labels = [str(x) for x in tools_filter_cli if x]
        return (False if labels else True), (labels if labels else [])
    tools = (agent_conf or {}).get("tools") if isinstance(agent_conf, dict) else None
    if isinstance(tools, list):
        if len(tools) == 0:
            return True, []
        labels = [str(x) for x in tools if x]
        return False, labels
    return False, None
