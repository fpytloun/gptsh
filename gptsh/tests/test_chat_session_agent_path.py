import pytest

from gptsh.core.agent import Agent
from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.session import ChatSession


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, params):
        self.calls.append(params)
        return self.responses.pop(0)

    async def stream(self, params):
        yield ""


class FakeMCP:
    async def start(self):
        pass

    async def call_tool(self, server, tool, args):
        return ""


@pytest.mark.asyncio
async def test_chat_session_from_agent_uses_agent_llm_and_policy():
    # First LLM response is final content (no tools)
    resp_final = {"choices": [{"message": {"content": "hello"}}]}
    fake_llm = FakeLLM([resp_final])
    policy = DefaultApprovalPolicy({"*": ["*"]})
    agent = Agent(name="test", llm=fake_llm, tools={}, policy=policy, generation_params={})

    session = ChatSession.from_agent(agent, progress=None, config={}, mcp=None)
    await session.start()
    out = await session.run("hi", provider_conf={"model": "x"})
    assert out == "hello"
    assert fake_llm.calls and fake_llm.calls[0]["model"] == "x"

