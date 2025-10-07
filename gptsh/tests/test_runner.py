import asyncio


async def _call_run_turn(**kwargs):
    from gptsh.core.runner import run_turn

    await run_turn(**kwargs)


def test_runner_stream_fallback_when_tool_delta_no_text(monkeypatch):
    # Arrange a Dummy ChatSession that streams no text but indicates tool deltas
    import gptsh.core.runner as runner_mod

    called = {"run_prompt": False}

    class DummyLLM:
        def get_last_stream_info(self):
            return {"saw_tool_delta": True, "tool_names": ["fs__read"]}

    class DummySession:
        def __init__(self, *a, **k):
            self._llm = DummyLLM()

        @classmethod
        def from_agent(cls, *a, **k):
            return cls()

        async def prepare_stream(self, *, prompt, provider_conf, agent_conf, cli_model_override, history_messages):
            params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
            return params, provider_conf.get("model")

        async def stream_with_params(self, params):
            if False:
                yield ""  # pragma: no cover

    async def fake_run_prompt_with_agent(**k):
        called["run_prompt"] = True
        return "tool-result"

    monkeypatch.setattr(runner_mod, "ChatSession", DummySession)
    monkeypatch.setattr(runner_mod, "run_prompt_with_agent", fake_run_prompt_with_agent)

    # Prepare request
    agent = object()
    prompt = "do something"
    config = {}
    provider_conf = {"model": "m"}
    result_sink = []

    # Act
    asyncio.run(
        _call_run_turn(
            agent=agent,
            prompt=prompt,
            config=config,
            provider_conf=provider_conf,
            agent_conf=None,
            cli_model_override=None,
            stream=True,
            progress=False,
            output_format="text",
            no_tools=False,
            logger=None,
            history_messages=None,
            result_sink=result_sink,
        )
    )

    # Assert: fallback path executed non-stream turn
    assert called["run_prompt"] is True
    assert result_sink and result_sink[0] == "tool-result"


def test_runner_stream_happy_path_output(monkeypatch, capsys):
    import gptsh.core.runner as runner_mod

    called = {"run_prompt": False}

    class DummyLLM:
        def get_last_stream_info(self):
            return {"saw_tool_delta": False}

    class DummySession:
        def __init__(self, *a, **k):
            self._llm = DummyLLM()

        @classmethod
        def from_agent(cls, *a, **k):
            return cls()

        async def prepare_stream(self, *, prompt, provider_conf, agent_conf, cli_model_override, history_messages):
            params = {"model": provider_conf.get("model"), "messages": [{"role": "user", "content": prompt}], "drop_params": True}
            return params, provider_conf.get("model")

        async def stream_with_params(self, params):
            yield "hello"
            yield " "
            yield "world"

    async def fake_run_prompt_with_agent(**k):
        called["run_prompt"] = True
        return "should-not-be-called"

    monkeypatch.setattr(runner_mod, "ChatSession", DummySession)
    monkeypatch.setattr(runner_mod, "run_prompt_with_agent", fake_run_prompt_with_agent)

    agent = object()
    prompt = "hi"
    config = {}
    provider_conf = {"model": "m"}
    result_sink = []

    asyncio.run(
        _call_run_turn(
            agent=agent,
            prompt=prompt,
            config=config,
            provider_conf=provider_conf,
            agent_conf=None,
            cli_model_override=None,
            stream=True,
            progress=False,
            output_format="text",
            no_tools=False,
            logger=None,
            history_messages=None,
            result_sink=result_sink,
        )
    )

    captured = capsys.readouterr()
    assert "hello world" in captured.out
    assert not called["run_prompt"]
    assert result_sink and result_sink[0] == "hello world"

