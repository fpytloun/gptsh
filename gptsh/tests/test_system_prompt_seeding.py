import pytest

from gptsh.core.session import ChatSession


class _DummyAgent:
    def __init__(self, name: str = "default") -> None:
        self.name = name
        # Minimal stub; ChatSession.from_agent won't call llm methods in this test
        self.llm = object()
        self.policy = object()
        self.tool_specs = []


@pytest.mark.asyncio
async def test_chat_session_from_agent_seeds_system_prompt():
    cfg = {
        "default_agent": "dev",
        "agents": {
            "dev": {
                "prompt": {"system": "SYS_PROMPT"},
            }
        },
    }
    agent = _DummyAgent(name="dev")
    sess = ChatSession.from_agent(agent, progress=None, config=cfg, mcp=None)
    # Should seed system prompt as first message
    assert isinstance(sess.history, list)
    assert sess.history and sess.history[0] == {"role": "system", "content": "SYS_PROMPT"}


@pytest.mark.asyncio
async def test_build_agent_appends_mcp_instructions_into_session(monkeypatch):
    # Monkeypatch tool resolution to avoid external deps
    import gptsh.core.config_resolver as resolver

    async def _fake_resolve_tools(cfg, allowed_servers=None):
        return {}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", _fake_resolve_tools)
    monkeypatch.setattr("gptsh.llm.tool_adapter.build_llm_tools_from_handles", lambda tools: [])

    async def _fake_discover(_cfg):
        return {"srv": "INSTR_BODY"}

    monkeypatch.setattr("gptsh.mcp.client._discover_server_instructions_async", _fake_discover)

    cfg = {
        "providers": {"openai": {"model": "m"}},
        "default_provider": "openai",
        "agents": {"default": {"model": "m"}},
        "default_agent": "default",
    }

    agent = await resolver.build_agent(cfg)
    # Session should exist and have a system prompt seeded with MCP instructions content
    sess = agent.session
    assert sess is not None
    assert sess.history
    first = sess.history[0]
    assert first.get("role") == "system"
    text = str(first.get("content") or "")
    assert "MCP server instructions" in text
    assert "Server: srv" in text
    assert "INSTR_BODY" in text
