import pytest

from gptsh.core.session import ChatSession
from gptsh.core.approval import DefaultApprovalPolicy


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, params):
        self.calls.append(params)
        return self.responses.pop(0)

    async def stream(self, params):  # not used in this test
        yield ""


class FakeMCP:
    def __init__(self, tools, results):
        self._tools = tools
        self._results = results
        self.called = []

    async def start(self):
        pass

    async def list_tools(self):
        return self._tools

    async def call_tool(self, server, tool, args):
        self.called.append((server, tool, args))
        key = f"{server}__{tool}"
        return self._results.get(key, "")

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_chat_session_tool_loop_auto_approved():
    # First response requests a tool call; second returns final content.
    resp_tool = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "function": {"name": "fs__read", "arguments": "{\"path\": \"/tmp/x\"}"},
                        }
                    ],
                }
            }
        ]
    }
    resp_final = {"choices": [{"message": {"content": "done"}}]}
    llm = FakeLLM([resp_tool, resp_final])
    mcp = FakeMCP({"fs": ["read"]}, {"fs__read": "content-of-file"})
    approval = DefaultApprovalPolicy({"fs": ["read"]})

    session = ChatSession(llm, mcp, approval, progress=None, config={})
    await session.start()
    out = await session.run("hi", provider_conf={"model": "x"})
    assert out == "done"
    assert mcp.called == [("fs", "read", {"path": "/tmp/x"})]

