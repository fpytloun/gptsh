import pytest

from typing import Any, Dict, List, Optional

from gptsh.core.agent import ToolHandle
from gptsh.core.config_resolver import build_agent


@pytest.mark.asyncio
async def test_build_agent_base_params_and_tools_filter(monkeypatch):
    # Arrange config with provider + agent; agent has model + params
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "default_provider": "openai",
        "providers": {
            "openai": {"model": "prov-model"},
        },
        "agents": {
            "dev": {
                "model": "agent-model",
                "params": {"temperature": 0.2},
                "tools": ["fs"],
            }
        },
    }

    captured_allowed: Optional[List[str]] = None

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        nonlocal captured_allowed
        captured_allowed = list(allowed_servers or []) if allowed_servers is not None else None
        # Return a minimal ToolHandle map
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return f"{server}__{name}::{args}"

        return {
            "fs": [ToolHandle(server="fs", name="read", description="", input_schema={}, _executor=_exec)],
        }

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)

    # Act: CLI override should win over agent/provider model
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai", cli_model_override="cli-model")

    # Assert base params and allowed servers filter
    assert getattr(agent.llm, "_base", {}).get("model") == "cli-model"
    assert getattr(agent.llm, "_base", {}).get("temperature") == 0.2
    assert captured_allowed == ["fs"]
    assert set(agent.tools.keys()) == {"fs"}
    assert len(agent.tools["fs"]) == 1

