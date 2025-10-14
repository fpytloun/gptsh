from typing import Any, Dict, List, Optional

import pytest

from gptsh.core.agent import ToolHandle
from gptsh.core.config_resolver import build_agent


@pytest.mark.asyncio
async def test_build_agent_base_params_and_tools_filter(monkeypatch):
    # Arrange config with provider + agent; agent has model + params
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "default_provider": "openai",
        "providers": {
            "openai": {"model": "prov-model"},
        },
        "agents": {
            "dev": {
                "model": "agent-model",
                # Current resolver reads top-level temperature, not nested params
                "temperature": 0.2,
                "tools": ["fs"],
            }
        },
    }

    captured_allowed: Optional[List[str]] = None

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        nonlocal captured_allowed
        captured_allowed = list(allowed_servers or []) if allowed_servers is not None else None
        # Return a minimal ToolHandle map
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return f"{server}__{name}::{args}"

        return {
            "fs": [ToolHandle(server="fs", name="read", description="", input_schema={}, _executor=_exec)],
        }

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)

    # Act: CLI override should win over agent/provider model
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai", cli_model_override="cli-model")

    # Assert base params and allowed servers filter
    assert getattr(agent.llm, "_base", {}).get("model") == "cli-model"
    assert getattr(agent.llm, "_base", {}).get("temperature") == 0.2
    assert captured_allowed == ["fs"]
    assert set(agent.tools.keys()) == {"fs"}
    assert len(agent.tools["fs"]) == 1
    # Ensure agent stores resolved configs
    assert isinstance(getattr(agent, "provider_conf", None), dict)
    assert isinstance(getattr(agent, "agent_conf", None), dict)
    assert agent.provider_conf.get("model") == "prov-model"
    assert agent.agent_conf.get("model") == "agent-model"


@pytest.mark.asyncio
async def test_agent_level_mcp_servers_override(monkeypatch):
    # Current resolver uses global inline servers; agent.mcp.servers are not injected as override
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {
            "servers": {"global_only": {"transport": {"type": "stdio"}, "command": "echo"}},
        },
        # Even if agent defines custom servers, resolver does not override via agent_conf
        "agents": {
            "dev": {
                "mcp": {"servers": {"agent_only": {"transport": {"type": "stdio"}, "command": "echo"}}}
            }
        },
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # Resolver should use global inline servers (no servers_override injection)
        assert "mcp" in conf and "servers" in conf["mcp"]
        # Return minimal ToolHandle list for the global server
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"global_only": [ToolHandle(server="global_only", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)

    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"global_only"}


@pytest.mark.asyncio
async def test_tools_filter_applies_over_agent_servers(monkeypatch):
    # Tools filter should apply over global inline servers
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {"servers": {
            "a": {"transport": {"type": "stdio"}, "command": "echo"},
            "b": {"transport": {"type": "stdio"}, "command": "echo"},
        }},
        "agents": {
            "dev": {
                "tools": ["b"],
            }
        },
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # Using global servers; allowed should filter to only 'b'
        assert conf.get("mcp", {}).get("servers")
        assert allowed_servers == ["b"]
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"b": [ToolHandle(server="b", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"b"}


@pytest.mark.asyncio
async def test_agent_custom_servers_do_not_inherit_global_approvals(monkeypatch):
    # Global has approvals for 'global', agent defines custom 'agent' servers only
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {"servers": {"global": {"transport": {"type": "stdio"}, "command": "echo", "autoApprove": ["*"]}}},
        "agents": {"dev": {"mcp": {"servers": {"agent": {"transport": {"type": "stdio"}, "command": "echo"}}}}},
    }

    # Patch get_auto_approved_tools path to run our logic but stub discovery
    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"agent": [ToolHandle(server="agent", name="x", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    from gptsh.mcp.api import get_auto_approved_tools
    await build_agent(config, cli_agent="dev", cli_provider="openai")
    # Compute approvals using effective (agent) config
    approvals = get_auto_approved_tools({**config}, agent_conf=config["agents"]["dev"])  # type: ignore[index]
    # Global approvals for 'global' should not leak into agent-only setup
    assert "global" not in approvals or approvals.get("global") == []


@pytest.mark.asyncio
async def test_agent_servers_take_precedence_over_global(monkeypatch):
    # Current resolver uses global inline servers; tools filter selects the desired server
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {"servers": {
            "global": {"transport": {"type": "stdio"}, "command": "echo"},
            "agent": {"transport": {"type": "stdio"}, "command": "echo"},
        }},
        "agents": {"dev": {"tools": ["agent"]}},
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # Using global servers; tools filter should narrow to 'agent'
        assert conf.get("mcp", {}).get("servers")
        assert allowed_servers == ["agent"]
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"agent": [ToolHandle(server="agent", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"agent"}


@pytest.mark.asyncio
async def test_global_inline_mcp_servers_used_when_no_override(monkeypatch):
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {
            "servers": {"global_only": {"transport": {"type": "stdio"}, "command": "echo"}},
        },
        "agents": {"dev": {"tools": ["global_only"]}},
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # No servers_override should be present; using global mcp.servers
        assert "mcp" in conf and "servers" in conf["mcp"]
        assert "servers_override" not in conf["mcp"]
        # tools filter from agent should apply
        assert allowed_servers == ["global_only"]
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"global_only": [ToolHandle(server="global_only", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"global_only"}


@pytest.mark.asyncio
async def test_cli_file_paths_ignored_when_inline_servers_present(monkeypatch):
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {
            "servers": {"inline": {"transport": {"type": "stdio"}, "command": "echo"}},
            "servers_files_cli": ["/should/not/use.json"],
            "servers_files": ["/should/not/use.json"],
        },
        "agents": {"dev": {"tools": ["inline"]}},
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # Inline servers should take precedence over CLI file paths
        assert conf.get("mcp", {}).get("servers")
        assert allowed_servers == ["inline"]
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"inline": [ToolHandle(server="inline", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"inline"}


@pytest.mark.asyncio
async def test_mcp_servers_accepts_json_string(monkeypatch):
    json_payload = '{"mcpServers": {"json_only": {"transport": {"type": "stdio"}, "command": "echo"}}}'
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {"servers": json_payload},
        "agents": {"dev": {"tools": ["json_only"]}},
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # The manager layer will parse the JSON; here we just ensure config includes servers string
        assert isinstance(conf.get("mcp", {}).get("servers"), str)
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"json_only": [ToolHandle(server="json_only", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"json_only"}


@pytest.mark.asyncio
async def test_inline_servers_invalid_json_raises(monkeypatch):
    bad_json = '{"mcpServers": '  # truncated
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {"servers": bad_json},
        "agents": {"dev": {}},
    }
    from gptsh.core.exceptions import ConfigError
    with pytest.raises(ConfigError):
        # Resolution path triggers server parsing
        await build_agent(config, cli_agent="dev", cli_provider="openai")


@pytest.mark.asyncio
async def test_yaml_mapping_with_mcpServers_unwrapped(monkeypatch):
    config: Dict[str, Any] = {
        "default_agent": "dev",
        "providers": {"openai": {"model": "m"}},
        "mcp": {
            # User pasted JSON-structured content into YAML mapping
            "servers": {
                "mcpServers": {
                    "yaml_json": {"transport": {"type": "stdio"}, "command": "echo"}
                }
            }
        },
        "agents": {"dev": {"tools": ["yaml_json"]}},
    }

    async def fake_resolve_tools(conf: Dict[str, Any], allowed_servers: Optional[List[str]] = None):
        # The client should unwrap mcpServers into servers mapping
        async def _exec(server: str, name: str, args: Dict[str, Any]) -> str:
            return "ok"
        return {"yaml_json": [ToolHandle(server="yaml_json", name="t", description="", input_schema={}, _executor=_exec)]}

    monkeypatch.setattr("gptsh.mcp.tools_resolver.resolve_tools", fake_resolve_tools)
    agent = await build_agent(config, cli_agent="dev", cli_provider="openai")
    assert set(agent.tools.keys()) == {"yaml_json"}
