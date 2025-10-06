from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from gptsh.core.approval import DefaultApprovalPolicy
from gptsh.interfaces import ApprovalPolicy


class ToolHandle:
    def __init__(self, server: str, name: str, description: str, input_schema: Dict[str, Any], *, _executor) -> None:
        self.server = server
        self.name = name
        self.description = description
        self.input_schema = input_schema
        # _executor: async callable (server, name, args) -> str
        self._executor = _executor

    async def invoke(self, arguments: Dict[str, Any]) -> str:
        return await self._executor(self.server, self.name, arguments)

    def __repr__(self) -> str:
        return f"ToolHandle(server={self.server!r}, name={self.name!r})"


@dataclass
class Agent:
    name: str
    llm: Any  # LiteLLMClient instance
    tools: Dict[str, List[ToolHandle]] = field(default_factory=dict)
    tool_specs: List[Dict[str, Any]] = field(default_factory=list)
    policy: ApprovalPolicy = field(default_factory=lambda: DefaultApprovalPolicy({}))
    generation_params: Dict[str, Any] = field(default_factory=dict)
    provider_conf: Dict[str, Any] = field(default_factory=dict)
    agent_conf: Dict[str, Any] = field(default_factory=dict)
