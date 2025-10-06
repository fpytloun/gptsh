from gptsh.core.models import map_config_to_models, pick_effective_agent_provider


def test_map_and_pick_models():
    cfg = {
        "default_agent": "chat",
        "default_provider": "openai",
        "providers": {
            "openai": {"model": "gpt-4o", "api_key_env": "OPENAI_API_KEY"},
            "azure": {"model": "gpt-4o-azure", "endpoint": "x"},
        },
        "agents": {
            "chat": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "prompt": {"system": "hi"},
                "params": {"temperature": 0.2},
                "mcp": {"tool_choice": "auto"},
                "tools": ["fs"],
                "output": "markdown",
            }
        },
    }

    defaults, providers, agents = map_config_to_models(cfg)
    assert defaults.default_agent == "chat"
    assert "openai" in providers and providers["openai"].model == "gpt-4o"
    assert agents["chat"].prompt.system == "hi"
    agent, provider = pick_effective_agent_provider(defaults, providers, agents)
    assert agent.name == "chat"
    assert provider.name == "openai"
    # CLI overrides
    agent2, provider2 = pick_effective_agent_provider(defaults, providers, agents, cli_agent="chat", cli_provider="azure")
    assert provider2.name == "azure"
