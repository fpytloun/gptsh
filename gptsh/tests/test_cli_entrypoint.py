import types
import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("tools_map", [
    ({"fs": ["read", "write"], "time": ["now"]}),
])
def test_cli_list_tools(monkeypatch, tools_map):
    from gptsh.cli.entrypoint import main

    # Stub list_tools
    import gptsh.cli.entrypoint as ep
    monkeypatch.setattr(ep, "list_tools", lambda cfg: tools_map)

    # Provide an empty agents config to avoid mis-detection
    def fake_load_config(paths=None):
        return {"agents": {"default": {}}, "default_agent": "default"}

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    runner = CliRunner()
    result = runner.invoke(main, ["--list-tools"])  # no providers required for listing
    assert result.exit_code == 0
    # Check that each server appears in output
    for server in tools_map:
        assert server in result.output


def test_cli_stream_no_tools(monkeypatch):
    from gptsh.cli.entrypoint import main
    import gptsh.cli.entrypoint as ep

    # Minimal config with a provider
    def fake_load_config(paths=None):
        return {"providers": {"openai": {"model": "x"}}, "default_provider": "openai", "agents": {"default": {}}, "default_agent": "default"}

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Stub prepare_completion_params to avoid building tools and to set chosen_model
    async def fake_prepare_completion_params(prompt, provider_conf, agent_conf, cli_model_override, config, no_tools, history_messages=None):
        params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
        return params, False, provider_conf.get("model")

    monkeypatch.setattr(ep, "prepare_completion_params", fake_prepare_completion_params)

    # Fake stream_completion to yield two chunks
    async def fake_stream_completion(params):
        yield "hello "
        yield "world"

    monkeypatch.setattr(ep, "stream_completion", fake_stream_completion)

    runner = CliRunner()
    result = runner.invoke(main, ["--no-tools", "--output", "text", "hi there"])
    assert result.exit_code == 0
    assert "hello world" in result.output


def test_cli_agent_provider_selection(monkeypatch):
    from gptsh.cli.entrypoint import main
    import gptsh.cli.entrypoint as ep

    # Config with two providers and two agents
    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}, "azure": {"model": "m2"}},
            "default_provider": "openai",
            "agents": {"default": {"provider": "openai"}, "dev": {"provider": "azure", "model": "m2"}},
            "default_agent": "default",
        }

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Short-circuit LLM path: streaming disabled by --no-tools and stub streaming
    async def fake_prepare_completion_params(prompt, provider_conf, agent_conf, cli_model_override, config, no_tools, history_messages=None):
        params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
        return params, False, provider_conf.get("model")

    async def fake_stream_completion(params):
        yield "x"

    monkeypatch.setattr(ep, "prepare_completion_params", fake_prepare_completion_params)
    monkeypatch.setattr(ep, "stream_completion", fake_stream_completion)

    runner = CliRunner()
    # Select non-default agent and provider override
    result = runner.invoke(main, ["--no-tools", "--agent", "dev", "--provider", "azure", "hello"])
    assert result.exit_code == 0
