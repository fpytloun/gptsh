import pytest

from gptsh.core.agent import Agent
from gptsh.core.api import run_prompt_with_agent
from gptsh.core.approval import DefaultApprovalPolicy


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
    async def complete(self, params):
        return self.responses.pop(0)
    async def stream(self, params):
        yield ""


@pytest.mark.asyncio
async def test_run_prompt_with_agent_simple_no_tools(monkeypatch):
    # Avoid spinning up real MCP
    class DummyMCP:
        async def start(self):
            pass
    monkeypatch.setattr("gptsh.mcp.manager.MCPManager", lambda cfg: DummyMCP())

    resp = {"choices": [{"message": {"content": "ok"}}]}
    llm = FakeLLM([resp])
    agent = Agent(name="t", llm=llm, tools={}, policy=DefaultApprovalPolicy({}), generation_params={})
    out = await run_prompt_with_agent(
        agent=agent,
        prompt="hi",
        config={},
        provider_conf={"model": "m"},
        agent_conf={},
        no_tools=True,
    )
    assert out == "ok"

