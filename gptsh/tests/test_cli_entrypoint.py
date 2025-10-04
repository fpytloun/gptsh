import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("tools_map", [
    ({"fs": ["read", "write"], "time": ["now"]}),
])
def test_cli_list_tools(monkeypatch, tools_map):
    # Stub list_tools
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main
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
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    # Minimal config with a provider
    def fake_load_config(paths=None):
        return {"providers": {"openai": {"model": "x"}}, "default_provider": "openai", "agents": {"default": {}}, "default_agent": "default"}

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Monkeypatch ChatSession to control streaming
    class DummySession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def prepare_stream(self, prompt, provider_conf, agent_conf, cli_model_override, history_messages):
            params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
            return params, provider_conf.get("model")
        async def stream_with_params(self, params):
            yield "hello "
            yield "world"

    monkeypatch.setattr(ep, "ChatSession", DummySession)
    # Also stub build_agent to avoid dependency on providers
    class DummyAgent:
        llm = object()
        policy = object()
    async def fake_build_agent(cfg, **k):
        return DummyAgent()
    monkeypatch.setattr(ep, "build_agent", fake_build_agent)

    runner = CliRunner()
    result = runner.invoke(main, ["--no-tools", "--output", "text", "hi there"])
    assert result.exit_code == 0
    assert "hello world" in result.output


def test_cli_agent_provider_selection(monkeypatch):
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    # Config with two providers and two agents
    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}, "azure": {"model": "m2"}},
            "default_provider": "openai",
            "agents": {"default": {"provider": "openai"}, "dev": {"provider": "azure", "model": "m2"}},
            "default_agent": "default",
        }

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Short-circuit LLM path via ChatSession monkeypatch
    class DummySession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def prepare_stream(self, prompt, provider_conf, agent_conf, cli_model_override, history_messages):
            params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
            return params, provider_conf.get("model")
        async def stream_with_params(self, params):
            yield "x"
        async def start(self):
            pass
        async def run(self, *a, **k):
            return ""
    monkeypatch.setattr(ep, "ChatSession", DummySession)
    class DummyAgent:
        llm = object()
        policy = object()
    async def fake_build_agent(cfg, **k):
        return DummyAgent()
    monkeypatch.setattr(ep, "build_agent", fake_build_agent)

    runner = CliRunner()
    # Select non-default agent and provider override
    result = runner.invoke(main, ["--no-tools", "--agent", "dev", "--provider", "azure", "hello"])
    assert result.exit_code == 0


def test_cli_list_agents(monkeypatch):
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}},
            "default_provider": "openai",
            "agents": {
                "default": {"model": "m1", "tools": ["fs"], "prompt": {"system": "S"}},
                "reviewer": {"provider": "openai", "model": "m1", "tools": []},
            },
            "default_agent": "default",
        }

    # Stub list_tools
    monkeypatch.setattr(ep, "list_tools", lambda cfg: {"fs": ["read"]})
    monkeypatch.setattr(ep, "get_auto_approved_tools", lambda cfg, agent_conf=None: {"*": ["*"]})

    runner = CliRunner()
    result = runner.invoke(main, ["--list-agents"])
    assert result.exit_code == 0
    assert "Configured agents:" in result.output
    assert "- default" in result.output
    # At least the default agent is listed; other sample agents may appear


def test_cli_tool_approval_denied_exit_code(monkeypatch):
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}},
            "default_provider": "openai",
            "agents": {"default": {"model": "m1", "mcp": {"tool_choice": "required"}}},
            "default_agent": "default",
        }

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Monkeypatch core API to simulate denial exception in non-stream path
    # Simulate tool approval denied by having run_llm path raise it via ChatSession.run
    from gptsh.core.exceptions import ToolApprovalDenied
    class DenySession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def start(self):
            pass
        async def prepare_stream(self, *a, **k):
            return ({"model": "m1", "messages": []}, "m1")
        async def run(self, *a, **k):
            raise ToolApprovalDenied("fs__delete")
    monkeypatch.setattr(ep, "ChatSession", DenySession)
    import gptsh.core.api as api
    monkeypatch.setattr(api, "ChatSession", DenySession)
    class DummyAgent:
        llm = object()
        policy = object()
    async def fake_build_agent(cfg, **k):
        return DummyAgent()
    monkeypatch.setattr(ep, "build_agent", fake_build_agent)

    # Avoid potential progress setup in non-tty
    runner = CliRunner()
    result = runner.invoke(main, ["--output", "text", "delete file"], catch_exceptions=False)
    assert result.exit_code == 4
    assert "Tool approval denied" in result.output


def test_cli_timeout_exit_code(monkeypatch):
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}},
            "default_provider": "openai",
            "agents": {"default": {"model": "m1"}},
            "default_agent": "default",
        }

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    class TimeoutSession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def start(self):
            pass
        async def prepare_stream(self, *a, **k):
            return ({"model": "m1", "messages": []}, "m1")
        def stream_with_params(self, params):
            # Simulate a timeout by raising from an async generator
            async def _gen():
                import asyncio
                raise asyncio.TimeoutError()
                yield ""  # unreachable
            return _gen()
        async def run(self, *a, **k):
            # Ensure that if non-stream path accidentally used, also timeout
            import asyncio
            raise asyncio.TimeoutError()

    monkeypatch.setattr(ep, "ChatSession", TimeoutSession)
    async def fake_build_agent(cfg, **k):
        return object()
    monkeypatch.setattr(ep, "build_agent", fake_build_agent)

    runner = CliRunner()
    # Force streaming path to be used
    result = runner.invoke(main, ["--no-tools", "--output", "text", "hello"], catch_exceptions=False)
    assert result.exit_code == 124
    assert "Operation timed out" in result.output


def test_cli_interactive_invokes_agent_repl(monkeypatch):
    import gptsh.cli.entrypoint as ep
    from gptsh.cli.entrypoint import main

    # Minimal config with agents/providers
    def fake_load_config(paths=None):
        return {
            "providers": {"openai": {"model": "m1"}},
            "default_provider": "openai",
            "agents": {"default": {"model": "m1"}},
            "default_agent": "default",
        }

    monkeypatch.setattr(ep, "load_config", fake_load_config)

    # Pretend we are on a TTY for interactive mode
    monkeypatch.setattr(ep.sys.stdout, "isatty", lambda: True)
    # No stdin content
    monkeypatch.setattr(ep, "read_stdin", lambda: None)

    # Stub build_agent to avoid external calls
    class DummyAgent:
        name = "default"
        llm = type("_", (), {"_base": {"model": "m1"}})()
        tools = {}
        policy = object()
        provider_conf = {"model": "m1"}
        agent_conf = {"model": "m1"}

    async def fake_build_agent(cfg, **k):
        return DummyAgent()

    called = {}

    def fake_run_agent_repl(**kwargs):
        called.update(kwargs)
        # Simulate immediate REPL exit without blocking
        return None

    monkeypatch.setattr(ep, "build_agent", fake_build_agent)
    monkeypatch.setattr(ep, "run_agent_repl", fake_run_agent_repl)

    runner = CliRunner()
    result = runner.invoke(main, ["-i", "--no-tools"], catch_exceptions=False)
    assert result.exit_code == 0
    # Verify REPL was invoked with an Agent and flags propagated
    assert isinstance(called.get("agent"), DummyAgent.__class__) or hasattr(called.get("agent"), "llm")
    assert called.get("stream") in {True, False}
    assert called.get("output_format") in {"markdown", "text"}
