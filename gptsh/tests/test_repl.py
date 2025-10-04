import pytest

from gptsh.core.repl import (
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
        provider_conf={"model": "ns/m1"},
        agent_conf={},
        cli_model_override=None,
        readline_enabled=False,
    )
    assert ">" in prompt and "dev" in prompt and "m1" in prompt


def test_command_model_updates_override_and_prompt():
    new_override, new_prompt = command_model(
        "org/new-model",
        agent_conf={},
        provider_conf={"model": "old"},
        cli_model_override=None,
        agent_name="tester",
        readline_enabled=False,
    )
    assert new_override == "org/new-model"
    assert "new-model" in new_prompt


def test_command_reasoning_effort_sets_and_validates():
    updated = command_reasoning_effort("high", {})
    assert updated["reasoning_effort"] == "high"
    with pytest.raises(ValueError):
        command_reasoning_effort("invalid", {})


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
    import gptsh.core.repl as repl
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
        readline_enabled=False,
    )
    assert agent_name == "dev"
    assert no_tools is True  # tools disabled due to empty list
    assert config["mcp"].get("allowed_servers") == []
    assert ">" in prompt_str and "dev" in prompt_str


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
