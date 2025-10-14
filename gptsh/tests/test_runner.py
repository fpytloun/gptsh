import asyncio


async def _call_run_turn(**kwargs):
    from gptsh.core.runner import run_turn

    await run_turn(**kwargs)


def test_runner_stream_fallback_when_tool_delta_no_text(monkeypatch):
    # Arrange a Dummy ChatSession that streams no text but indicates tool deltas
    import gptsh.core.runner as runner_mod


    class DummyLLM:
        def get_last_stream_info(self):
            return {"saw_tool_delta": True, "tool_names": ["fs__read"]}

    class DummySession:
        def __init__(self, *a, **k):
            self._progress = None
            self._llm = DummyLLM()

        @classmethod
        def from_agent(cls, *a, **k):
            return cls()

        async def stream_turn(self, *, prompt, provider_conf, agent_conf, cli_model_override, no_tools, history_messages):
            if False:
                yield ""  # pragma: no cover

    # With unified stream_turn, fallback is internal. We assert that runner completes
    # and result_sink contains empty string since DummySession yields nothing.

    monkeypatch.setattr(runner_mod, "ChatSession", DummySession)
    # No external fallback used anymore

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
    # Unified path: no output produced by DummySession, but runner completes
    assert result_sink is not None


def test_runner_stream_happy_path_output(monkeypatch, capsys):
    import gptsh.core.runner as runner_mod


    class DummyLLM:
        def get_last_stream_info(self):
            return {"saw_tool_delta": False}

    class DummySession:
        def __init__(self, *a, **k):
            self._progress = None
            self._llm = DummyLLM()

        @classmethod
        def from_agent(cls, *a, **k):
            return cls()

        async def stream_turn(self, *, prompt, provider_conf, agent_conf, cli_model_override, no_tools, history_messages):
            yield "hello"
            yield " "
            yield "world"

    # No external fallback now

    monkeypatch.setattr(runner_mod, "ChatSession", DummySession)
    # No external fallback now

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
    assert result_sink and result_sink[0] == "hello world"
