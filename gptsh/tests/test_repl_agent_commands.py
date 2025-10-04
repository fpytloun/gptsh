import pytest

from gptsh.core.agent import Agent
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.repl import command_model, command_no_tools, command_reasoning_effort, command_tools


class DummyLLM:
    def __init__(self, model):
        self._base = {"model": model}


class DummyHandle:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


@pytest.mark.asyncio
async def test_repl_commands_basic(monkeypatch):
    # Prepare a minimal Agent
    agent = Agent(
        name="default",
        llm=DummyLLM("m0"),
        tools={"fs": [DummyHandle("read", "Read file"), DummyHandle("write", "Write file")]},
        policy=DefaultApprovalPolicy({"fs": ["read"]}),
        generation_params={},
        provider_conf={"model": "m0"},
        agent_conf={"model": "m0"},
    )

    # /tools should include checkmark for auto-approved
    tools_out = command_tools(agent)
    assert "fs (2):" in tools_out
    assert "read" in tools_out and "\u2714" in tools_out

    # /model updates prompt and override
    cli_model_override, prompt = command_model(
        "m1",
        agent_conf=agent.agent_conf,
        provider_conf=agent.provider_conf,
        cli_model_override=None,
        agent_name=agent.name,
        readline_enabled=False,
    )
    assert cli_model_override == "m1"
    agent.llm._base["model"] = cli_model_override
    assert agent.llm._base["model"] == "m1"

    # /reasoning_effort validation and update
    agent_conf = command_reasoning_effort("medium", agent.agent_conf)
    assert agent_conf.get("reasoning_effort") == "medium"

    # /no-tools toggling using command_no_tools
    # Fake build_agent -> return same agent for simplicity
    # Monkey path the build_agent symbol used inside command_no_tools to a sync stub
    class _FakeModule:
        @staticmethod
        def build_agent(cfg, **k):
            async def _coro():
                return agent
            return _coro()

    # Patch the actual resolver function referenced by the import inside command_no_tools
    monkeypatch.setattr("gptsh.core.config_resolver.build_agent", _FakeModule.build_agent, raising=False)

    new_agent, new_no_tools, msg = command_no_tools(
        "on",
        config={"agents": {agent.name: {}}},
        agent_name=agent.name,
        cli_model_override=cli_model_override,
        current_no_tools=False,
    )
    assert isinstance(new_agent, Agent)
    assert new_no_tools is True
    assert "Tools disabled" in msg
