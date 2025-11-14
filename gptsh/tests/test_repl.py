import pytest

from gptsh.cli.repl import (
    ReplExit,
    build_prompt,
    command_agent,
    command_exit,
    command_help,
    command_model,
    command_reasoning_effort,
)


def test_build_prompt_contains_agent_and_model():
    prompt = build_prompt(
        agent_name="dev",
        model="ns/m1",
    )
    # prompt is ANSI formatted text, convert to string to check content
    prompt_str = str(prompt.value) if hasattr(prompt, "value") else str(prompt)
    assert ">" in prompt_str and "dev" in prompt_str and "m1" in prompt_str


def test_command_model_updates_override_and_prompt():
    from gptsh.core.agent import Agent
    from gptsh.core.approval import DefaultApprovalPolicy

    class DummyLLM:
        def __init__(self):
            self._base = {"model": "old"}

    agent = Agent(
        name="tester",
        llm=DummyLLM(),
        tools={},
        policy=DefaultApprovalPolicy({}),
        generation_params={},
    )
    new_override, new_prompt = command_model(
        "org/new-model",
        agent=agent,
        agent_name="tester",
    )
    assert new_override == "org/new-model"
    # new_prompt is ANSI formatted text
    prompt_str = str(new_prompt.value) if hasattr(new_prompt, "value") else str(new_prompt)
    assert "new-model" in prompt_str


def test_command_reasoning_effort_sets_and_validates():
    from gptsh.core.agent import Agent

    class DummyLLM:
        def __init__(self):
            self._base = {"model": "m"}

    from gptsh.core.approval import DefaultApprovalPolicy

    agent = Agent(
        name="a", llm=DummyLLM(), tools={}, policy=DefaultApprovalPolicy({}), generation_params={}
    )
    command_reasoning_effort("high", agent)
    assert agent.llm._base["reasoning_effort"] == "high"
    with pytest.raises(ValueError):
        command_reasoning_effort("invalid", agent)


def test_command_agent_switches_and_applies_policy(monkeypatch):
    # Setup config with two agents, and dev disables tools (empty list)
    config = {
        "agents": {
            "default": {"model": "m0"},
            "dev": {"model": "m1", "tools": []},
        },
        "mcp": {},
    }

    # Dummy loop that pretends to run coroutines
    class DummyLoop:
        def run_until_complete(self, fut):
            # pretend to run and return a result
            return None

    # mgr with async stop
    class DummyMgr:
        async def stop(self):
            return None

    # Ensure MCP start returns a placeholder (won't be used since tools disabled)
    import gptsh.cli.repl as repl

    monkeypatch.setattr(repl, "ensure_sessions_started_async", lambda cfg: None)

    agent_conf, prompt_str, agent_name, no_tools, mgr = command_agent(
        "dev",
        config=config,
        agent_conf={},
        agent_name="default",
        provider_conf={"model": "m0"},
        cli_model_override=None,
        no_tools=False,
        mgr=DummyMgr(),
        loop=DummyLoop(),
    )
    assert agent_name == "dev"
    assert no_tools is True  # tools disabled due to empty list
    assert config["mcp"].get("allowed_servers") == []
    # prompt_str is ANSI formatted text
    prompt_str_content = str(prompt_str.value) if hasattr(prompt_str, "value") else str(prompt_str)
    assert ">" in prompt_str_content and "dev" in prompt_str_content


def test_command_exit_raises():
    with pytest.raises(ReplExit):
        command_exit()


def test_command_help_lists_commands():
    text = command_help()
    assert "Available commands:" in text
    assert "/exit" in text and "/quit" in text
    assert "/model <name>" in text
    assert "/agent <name>" in text
    assert "/reasoning_effort" in text
