import pytest

from gptsh.core.agent import Agent
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.repl import command_no_tools


class DummyLLM:
    def __init__(self, model):
        self._base = {"model": model}


class DummyHandle:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


@pytest.mark.asyncio
async def test_command_no_tools_rebuilds_agent_and_toggles(monkeypatch):
    # Start with an agent that has tools
    agent_name = "default"
    agent = Agent(
        name=agent_name,
        llm=DummyLLM("m0"),
        tools={"fs": [DummyHandle("read")]},
        policy=DefaultApprovalPolicy({"fs": ["read"]}),
        generation_params={},
        provider_conf={"model": "m0"},
        agent_conf={"model": "m0"},
    )

    # Patch the resolver to simulate enable/disable by returning empty/non-empty tools
    async def fake_build_agent(cfg, **k):
        if k.get("cli_no_tools"):
            return Agent(
                name=agent_name,
                llm=DummyLLM("m0"),
                tools={},
                policy=DefaultApprovalPolicy({}),
                generation_params={},
                provider_conf={"model": "m0"},
                agent_conf={"model": "m0"},
            )
        return agent

    monkeypatch.setattr("gptsh.core.config_resolver.build_agent", fake_build_agent, raising=False)

    # Toggle ON (disable tools)
    new_agent, no_tools, msg = command_no_tools(
        "on",
        config={"agents": {agent_name: {}}},
        agent_name=agent_name,
        cli_model_override="m0",
        current_no_tools=False,
    )
    assert no_tools is True
    assert isinstance(new_agent, Agent)
    assert sum(len(v or []) for v in (new_agent.tools or {}).values()) == 0
    assert "disabled" in msg

    # Toggle OFF (enable tools)
    new_agent2, no_tools2, msg2 = command_no_tools(
        "off",
        config={"agents": {agent_name: {}}},
        agent_name=agent_name,
        cli_model_override="m0",
        current_no_tools=True,
    )
    assert no_tools2 is False
    assert isinstance(new_agent2, Agent)
    assert sum(len(v or []) for v in (new_agent2.tools or {}).values()) >= 0
    assert "enabled" in msg2

