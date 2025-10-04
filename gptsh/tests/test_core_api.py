import pytest


@pytest.mark.asyncio
async def test_core_run_prompt_monkey(monkeypatch):
    # Monkey ChatSession via module where it's imported
    import gptsh.core.api as api
    from gptsh.core.api import run_prompt_with_agent

    class DummySession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def start(self):
            pass
        async def run(self, *a, **k):
            return "ok"

    monkeypatch.setattr(api, "ChatSession", DummySession)
    monkeypatch.setattr(api, "MCPManager", lambda cfg: object())

    class DummyAgent:
        llm = object()
        policy = object()
    out = await run_prompt_with_agent(
        agent=DummyAgent(),
        prompt="hi",
        config={},
        provider_conf={"model": "m"},
        agent_conf={},
        cli_model_override=None,
        no_tools=True,
        history_messages=None,
        progress_reporter=None,
    )
    assert out == "ok"


@pytest.mark.asyncio
async def test_core_prepare_stream_params(monkeypatch):
    import gptsh.core.api as api
    from gptsh.core.api import prepare_stream_params

    class DummySession:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def from_agent(cls, agent, *, progress, config, mcp=None):
            return cls()
        async def prepare_stream(self, *a, **k):
            return ({"model": "m"}, "m")

    monkeypatch.setattr(api, "ChatSession", DummySession)

    class DummyAgent:
        llm = object()
        policy = object()
    params, model = await prepare_stream_params(
        agent=DummyAgent(),
        prompt="hi",
        config={},
        provider_conf={"model": "m"},
        agent_conf={},
        cli_model_override=None,
        history_messages=None,
        progress_reporter=None,
    )
    assert params["model"] == "m" and model == "m"
