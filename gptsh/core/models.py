from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ProviderConfig:
    name: str
    model: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentPrompt:
    system: Optional[str] = None
    user: Optional[str] = None


@dataclass
class AgentConfig:
    name: str
    provider: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    prompt: AgentPrompt = field(default_factory=AgentPrompt)
    mcp: Dict[str, Any] = field(default_factory=dict)
    tools: Optional[List[str]] = None  # None=all, []=disabled, [labels]=allow-list
    no_tools: bool = False
    output: Optional[str] = None  # text|markdown


@dataclass
class Defaults:
    default_agent: Optional[str] = None
    default_provider: Optional[str] = None


def _as_dict(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(d or {})


def map_config_to_models(config: Dict[str, Any]) -> Tuple[Defaults, Dict[str, ProviderConfig], Dict[str, AgentConfig]]:
    defaults = Defaults(
        default_agent=config.get("default_agent"),
        default_provider=config.get("default_provider"),
    )
    providers_conf = _as_dict(config.get("providers"))
    providers: Dict[str, ProviderConfig] = {}
    for name, p in providers_conf.items():
        if not isinstance(p, dict):
            p = {}
        providers[name] = ProviderConfig(
            name=name,
            model=p.get("model"),
            params=_as_dict(p),
        )
    agents_conf = _as_dict(config.get("agents"))
    agents: Dict[str, AgentConfig] = {}
    for name, a in agents_conf.items():
        if not isinstance(a, dict):
            a = {}
        prompt_cfg = _as_dict(a.get("prompt"))
        agents[name] = AgentConfig(
            name=name,
            provider=a.get("provider"),
            model=a.get("model"),
            reasoning_effort=a.get("reasoning_effort"),
            temperature=a.get("temperature"),
            prompt=AgentPrompt(system=prompt_cfg.get("system"), user=prompt_cfg.get("user")),
            tools=list(a.get("tools")) if isinstance(a.get("tools"), list) else None,
            no_tools=bool(a.get("no_tools", False)),
            output=a.get("output"),
        )
    return defaults, providers, agents


def pick_effective_agent_provider(
    defaults: Defaults,
    providers: Dict[str, ProviderConfig],
    agents: Dict[str, AgentConfig],
    *,
    cli_agent: Optional[str] = None,
    cli_provider: Optional[str] = None,
) -> Tuple[AgentConfig, ProviderConfig]:
    agent_name = cli_agent or defaults.default_agent or (next(iter(agents)) if agents else None)
    if not agent_name or agent_name not in agents:
        raise KeyError("agent not found")
    agent = agents[agent_name]
    provider_name = cli_provider or agent.provider or defaults.default_provider or (next(iter(providers)) if providers else None)
    if not provider_name or provider_name not in providers:
        raise KeyError("provider not found")
    provider = providers[provider_name]
    return agent, provider

