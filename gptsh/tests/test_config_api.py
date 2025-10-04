from gptsh.core.config_api import (
    compute_tools_policy,
    effective_output,
    select_agent_provider_dicts,
)


def test_select_agent_provider_dicts():
    cfg = {
        "default_agent": "chat",
        "default_provider": "openai",
        "providers": {"openai": {"model": "m"}},
        "agents": {"chat": {"provider": "openai", "model": "m"}},
    }
    agent_conf, provider_conf = select_agent_provider_dicts(cfg)
    assert provider_conf["model"] == "m"
    assert agent_conf["provider"] == "openai"


def test_effective_output():
    assert effective_output("text", {"output": "markdown"}) == "text"
    assert effective_output(None, {"output": "text"}) == "text"
    assert effective_output(None, {}) == "markdown"


def test_compute_tools_policy():
    assert compute_tools_policy({}, None, True) == (True, [])
    assert compute_tools_policy({}, ["fs"], False) == (False, ["fs"])
    assert compute_tools_policy({"tools": []}, None, False) == (True, [])
    assert compute_tools_policy({"tools": ["fs", "time"]}, None, False) == (False, ["fs", "time"])
    assert compute_tools_policy({}, None, False) == (False, None)

