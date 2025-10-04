from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ProviderConfig:
    name: str
    model: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    mcp: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentPrompt:
    system: Optional[str] = None
    user: Optional[str] = None


@dataclass
class AgentConfig:
    name: str
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: AgentPrompt = field(default_factory=AgentPrompt)
    params: Dict[str, Any] = field(default_factory=dict)
    mcp: Dict[str, Any] = field(default_factory=dict)
    tools: Optional[List[str]] = None  # None=all, []=disabled, [labels]=allow-list
    no_tools: bool = False
    output: Optional[str] = None  # text|markdown


@dataclass
class Defaults:
    default_agent: str
    default_provider: Optional[str]


def _as_dict(obj: Any) -> Dict[str, Any]:
    return obj if isinstance(obj, dict) else {}


def map_config_to_models(config: Dict[str, Any]) -> Tuple[Defaults, Dict[str, ProviderConfig], Dict[str, AgentConfig]]:
    providers_raw = _as_dict(config.get("providers"))
    providers: Dict[str, ProviderConfig] = {}
    for pname, pconf in providers_raw.items():
        pmap = _as_dict(pconf)
        providers[pname] = ProviderConfig(
            name=str(pname),
            model=pmap.get("model"),
            params={k: v for k, v in pmap.items() if k not in {"model", "mcp"}},
            mcp=_as_dict(pmap.get("mcp")),
        )

    agents_raw = _as_dict(config.get("agents"))
    agents: Dict[str, AgentConfig] = {}
    for aname, aconfd in agents_raw.items():
        amap = _as_dict(aconfd)
        prompt_map = _as_dict(amap.get("prompt"))
        agents[aname] = AgentConfig(
            name=str(aname),
            provider=amap.get("provider"),
            model=amap.get("model"),
            prompt=AgentPrompt(system=prompt_map.get("system"), user=prompt_map.get("user")),
            params=_as_dict(amap.get("params")),
            mcp=_as_dict(amap.get("mcp")),
            tools=list(amap.get("tools")) if isinstance(amap.get("tools"), list) else None,
            no_tools=bool(amap.get("no_tools", False)),
            output=amap.get("output"),
        )

    defaults = Defaults(
        default_agent=str(config.get("default_agent") or "default"),
        default_provider=config.get("default_provider"),
    )
    return defaults, providers, agents


def pick_effective_agent_provider(
    defaults: Defaults,
    providers: Dict[str, ProviderConfig],
    agents: Dict[str, AgentConfig],
    cli_agent: Optional[str] = None,
    cli_provider: Optional[str] = None,
) -> Tuple[AgentConfig, ProviderConfig]:
    agent_name = cli_agent or defaults.default_agent or "default"
    if agent_name not in agents:
        raise KeyError(f"Unknown agent: {agent_name}")
    agent = agents[agent_name]
    provider_name = cli_provider or agent.provider or defaults.default_provider or (next(iter(providers)) if providers else None)
    if provider_name is None or provider_name not in providers:
        raise KeyError(f"Unknown provider: {provider_name}")
    provider = providers[provider_name]
    return agent, provider

