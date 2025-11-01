from gptsh.core.config_api import (
    compute_tools_policy,
    effective_output,
    get_sessions_enabled,
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


def test_get_sessions_enabled_precedence():
    base_cfg = {"agents": {"default": {}}, "default_agent": "default"}
    # CLI flag disables regardless of config
    assert (
        get_sessions_enabled({**base_cfg, "sessions": {"enabled": True}}, no_sessions_cli=True)
        is False
    )
    # Agent override false beats global true
    agent_conf = {"sessions": {"enabled": False}}
    assert (
        get_sessions_enabled({**base_cfg, "sessions": {"enabled": True}}, agent_conf=agent_conf)
        is False
    )
    # Agent override true beats global false
    agent_conf_true = {"sessions": {"enabled": True}}
    assert (
        get_sessions_enabled(
            {**base_cfg, "sessions": {"enabled": False}}, agent_conf=agent_conf_true
        )
        is True
    )
    # Global only
    assert get_sessions_enabled({**base_cfg, "sessions": {"enabled": False}}) is False
    assert get_sessions_enabled({**base_cfg, "sessions": {"enabled": True}}) is True
    # Neither set => default True
    assert get_sessions_enabled(base_cfg) is True
