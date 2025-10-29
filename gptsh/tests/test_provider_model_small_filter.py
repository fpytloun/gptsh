import pytest

from gptsh.core.config_resolver import build_agent
from gptsh.core.sessions import resolve_small_model


@pytest.mark.asyncio
async def test_provider_model_small_not_in_llm_base(monkeypatch):
    # Minimal tool resolver to avoid external dependency
    async def fake_resolve_tools(conf, allowed_servers=None):
        return {}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)

    cfg = {
        "default_agent": "dev",
        "default_provider": "openai",
        "providers": {
            "openai": {
                "model": "big",
                "model_small": "small",
                # also include an unrelated key to ensure filtering works
                "random": 1,
            }
        },
        "agents": {"dev": {}},
    }

    agent = await build_agent(cfg, cli_agent="dev", cli_provider="openai")
    base = getattr(agent.llm, "_base", {})
    # model_small must NOT be passed to the LLM base params
    assert "model_small" not in base
    assert base.get("model") == "big"

    # However resolve_small_model should still see provider's model_small via provider config
    # We mimic how entrypoints use it: pass agent and provider dicts
    agent_conf = cfg["agents"]["dev"]
    provider_conf = cfg["providers"]["openai"]
    sm = resolve_small_model(agent_conf, provider_conf)
    assert sm == "small"
