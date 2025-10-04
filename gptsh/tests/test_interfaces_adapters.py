import pytest

from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.core.progress import RichProgressReporter
from gptsh.llm.litellm_client import LiteLLMClient
from gptsh.mcp.manager import MCPManager


@pytest.mark.asyncio
async def test_litellm_client_complete_monkeypatch(monkeypatch):
    captured = {}

    async def fake_acompletion(**params):  # type: ignore
        captured["params"] = params
        return {"choices": [{"message": {"content": "ok"}}]}

    import gptsh.llm.litellm_client as mod

    class Dummy:
        pass

    def fake_import(name):
        if name == "litellm":
            d = Dummy()
            d.acompletion = fake_acompletion
            return d
        raise ImportError

    monkeypatch.setitem(mod.__dict__, "__name__", mod.__name__)
    monkeypatch.setitem(mod.__dict__, "__loader__", __loader__)
    monkeypatch.setitem(mod.__dict__, "__package__", mod.__package__)
    monkeypatch.setitem(mod.__dict__, "__spec__", __spec__)

    # Monkeypatch the module-level import by overriding builtins.__import__ via importlib
    import builtins

    real_import = builtins.__import__

    def _imp(name, *a, **kw):
        if name == "litellm":
            return fake_import(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _imp)

    client = LiteLLMClient()
    resp = await client.complete({"model": "test", "messages": [{"role": "user", "content": "hi"}]})
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert captured["params"]["model"] == "test"


@pytest.mark.asyncio
async def test_mcp_manager_list_and_call_tool(monkeypatch):
    # Stub underlying functions used by the manager
    called = {
        "ensure": 0,
        "list": 0,
        "exec": [],
    }

    async def fake_ensure_started(cfg):
        called["ensure"] += 1

    def fake_list_tools(cfg):
        called["list"] += 1
        return {"fs": ["read", "write"]}

    async def fake_exec(server, tool, args, cfg):
        called["exec"].append((server, tool, args))
        return f"{server}:{tool}:{args.get('x', '')}"

    import gptsh.mcp.manager as mgr

    monkeypatch.setattr(mgr, "_ensure_started", fake_ensure_started)
    monkeypatch.setattr(mgr, "_list_tools", fake_list_tools)
    monkeypatch.setattr(mgr, "_execute_tool_async", fake_exec)

    m = MCPManager({})
    await m.start()
    tools = await m.list_tools()
    assert tools == {"fs": ["read", "write"]}
    out = await m.call_tool("fs", "read", {"x": "1"})
    assert out == "fs:read:1"
    assert called["ensure"] == 1
    assert called["list"] == 1
    assert called["exec"] == [("fs", "read", {"x": "1"})]


def test_default_approval_policy():
    p = DefaultApprovalPolicy({"*": ["*"], "fs": ["read"]})
    assert p.is_auto_allowed("fs", "read") is True
    assert p.is_auto_allowed("fs", "write") is True  # covered by global *
    p2 = DefaultApprovalPolicy({"fs": ["read_file", "fs__delete_file"]})
    assert p2.is_auto_allowed("fs", "read-file") is True
    assert p2.is_auto_allowed("fs", "delete_file") is True
    assert p2.is_auto_allowed("fs", "other") is False


def test_rich_progress_reporter_smoke():
    pr = RichProgressReporter()
    pr.start()
    tid = pr.add_task("doing work")
    pr.complete_task(tid, "done")
    pr.stop()

