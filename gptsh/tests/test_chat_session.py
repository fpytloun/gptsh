import pytest

from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.session import ChatSession


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


@pytest.mark.asyncio
async def test_chat_session_tool_loop_denied():
    resp_tool = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "function": {"name": "fs__delete", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }
    resp_final = {"choices": [{"message": {"content": "final"}}]}
    llm = FakeLLM([resp_tool, resp_final])
    mcp = FakeMCP({"fs": ["delete"]}, {"fs__delete": "ok"})
    # No approvals for delete
    approval = DefaultApprovalPolicy({})
    session = ChatSession(llm, mcp, approval, progress=None, config={})
    await session.start()
    out = await session.run("hi", provider_conf={"model": "x"})
    assert out == "final"
    # Tool should not be called because it was denied
    assert mcp.called == []


@pytest.mark.asyncio
async def test_chat_session_multiple_tools():
    resp_tool = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "function": {"name": "fs__read", "arguments": "{}"},
                        },
                        {
                            "id": "t2",
                            "function": {"name": "time__now", "arguments": "{}"},
                        },
                    ],
                }
            }
        ]
    }
    resp_final = {"choices": [{"message": {"content": "combined"}}]}
    llm = FakeLLM([resp_tool, resp_final])
    mcp = FakeMCP({"fs": ["read"], "time": ["now"]}, {"fs__read": "A", "time__now": "B"})
    approval = DefaultApprovalPolicy({"*": ["*"]})
    session = ChatSession(llm, mcp, approval, progress=None, config={})
    await session.start()
    out = await session.run("hi", provider_conf={"model": "x"})
    assert out == "combined"
    assert mcp.called == [("fs", "read", {}), ("time", "now", {})]
