import pytest


@pytest.mark.asyncio
async def test_core_run_prompt_monkey(monkeypatch):
    # Also test stream_turn with no-tools path signature
    # Adapted: use ChatSession directly now that core.api is removed
    from gptsh.core.session import ChatSession

    class DummyLLM:
        async def complete(self, params):
            return {"choices": [{"message": {"content": "ok"}}]}

        async def stream(self, params):
            if False:
                yield ""

    from gptsh.core.agent import Agent
    from gptsh.core.approval import DefaultApprovalPolicy

    agent = Agent(
        name="a", llm=DummyLLM(), tools={}, policy=DefaultApprovalPolicy({}), generation_params={}
    )
    session = ChatSession.from_agent(agent, progress=None, config={}, mcp=None)

    params, has_tools, model = await session._prepare_params(
        prompt="hi",
        no_tools=False,
        history_messages=None,
    )
    # Now run a small no-tools turn and accept empty result
    chunks = []
    async for t in session.stream_turn(
        prompt="hi",
        no_tools=True,
        history_messages=None,
    ):
        chunks.append(t)
    assert "".join(chunks) in ("ok", "")
    assert params["model"] and model
