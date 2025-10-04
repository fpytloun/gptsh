## Refactor: Modular, Extensible Architecture

### Overview

GPTSH will be refactored into a clean, modular structure to maximize readability, testability, and extensibility. The new layers and their responsibilities are:

- **Interfaces**: Define Protocols for core abstractions—LLM clients, MCP clients, approval policies, progress reporters, and UI entrypoints.
- **Domain Models**: Typed `@dataclass` definitions for AgentConfig, ProviderConfig, ToolSpec, ToolServerSpec, and session state.
- **LLM Adapters**: Encapsulate all LiteLLM interactions in `gptsh/llm/litellm_client.py` with `LiteLLMClient`, plus the tool-adapter in `gptsh/llm/tool_adapter.py`.
- **MCP Management**: Centralize server discovery, lifecycle, and tool invocation in `gptsh/mcp/manager.py` with `MCPManager`. Builtin tools use a decorator registry in `gptsh/mcp/builtin/base.py`.
- **Orchestration**: The `ChatSession` in `gptsh/core/session.py` drives message history, tool loops (parallel-capable), approvals, and progress updates.
- **Approval Policy**: `DefaultApprovalPolicy` in `gptsh/core/approval.py` handles wildcard auto-approval and interactive confirmations via Rich.
- **Progress Reporting**: `RichProgressReporter` in `gptsh/core/progress.py` provides persistent, wrapping-safe task lines.
- **UI Layer**: Abstract UI with `UIInterface` in `gptsh/interfaces.py`; implement CLI in `gptsh/ui/cli.py` and scaffold TUI in `gptsh/ui/tui.py`.
- **Configuration & Logging**: Keep the loader in `gptsh/config/loader.py` with !include and env-var expansion, and centralized logging/redaction in `gptsh/core/logging.py`.

This architecture ensures each component can be replaced or extended independently—swap LLM backends, add a web UI, or plug in new tool servers without touching unrelated code.

To support future extensibility—multiple front-ends (CLI, TUI, web), pluggable LLM backends, flexible MCP tool servers, and structured domain models—GPTSH will be reorganized into a modular, object-oriented architecture. Each layer exposes a clear interface (Protocol or abstract base class), and dedicated implementation modules fulfill those interfaces. This structure improves readability, testability, security, and maintainability, and enables independent replacement of components without impacting others.

**Core Components**

1. **Interfaces** (`gptsh/interfaces.py`):  
   - `LLMClient`: async `complete(params) -> Dict[str,Any]` and `stream(params) -> AsyncIterator[str]`.  
   - `MCPClient`: async `list_tools() -> Dict[str,List[str]]` and `call_tool(server,tool,args) -> str`.  
   - `ApprovalPolicy`: `is_auto_allowed(server,tool) -> bool` and async `confirm(server,tool,args) -> bool`.  
   - `ProgressReporter`: `start()`, `stop()`, `add_task(desc) -> Optional[int]`, `complete_task(id,desc)`, `pause()`, `resume()`.

2. **LLM Adapter** (`gptsh/llm/litellm_client.py`):  
   - `LiteLLMClient` implements `LLMClient`, centralizing all `litellm` calls and chunk parsing.

3. **MCP Manager** (`gptsh/mcp/manager.py`):  
   - `MCPManager` implements `MCPClient`, wrapping existing logic with a clean API: `start()`, `list_tools()`, `call_tool()`, `stop()`.

4. **Approvals** (`gptsh/core/approval.py`):  
   - `DefaultApprovalPolicy` implements `ApprovalPolicy`, encapsulating wildcard normalization and interactive confirmation via `rich.prompt`.

5. **Progress Reporting** (`gptsh/core/progress.py`):  
   - `RichProgressReporter` implements `ProgressReporter`, providing persistent, well-wrapped progress bars.

6. **Chat Orchestration** (`gptsh/core/session.py`):  
   - `ChatSession` class encapsulates conversation history, LLM calls, tool-call detection, parallel execution, approvals, and progress updates—replacing `complete_with_tools`.

7. **Domain Models** (`gptsh/domain/models.py`):  
   - Typed `@dataclass` definitions (`ProviderConfig`, `AgentConfig`, `ToolSpec`, etc.), mapping raw config dicts to structured objects.

8. **UI Layer** (`gptsh/ui`):  
  - `UIInterface` protocol.  
  - `cli/entrypoint.py` implements the CLI wiring using the interfaces; a future `gptsh/ui/tui.py` can implement a TUI.

9. **Builtin Tool Registry** (`gptsh/mcp/builtin/base.py`):  
  - Decorator-based `@tool` registration and shared `list_tools`, `list_tools_detailed`, and `execute` dispatch.  
  - Individual builtin modules only declare tool functions with metadata.

### Current Status (as of 2025-10-04)

- Implemented interfaces: `LLMClient`, `MCPClient`, `ApprovalPolicy`, `ProgressReporter`.
- Added adapters: `LiteLLMClient`, `MCPManager`.
- Added orchestration: `ChatSession` (non-stream + streaming helpers).
- Added approval policy: `DefaultApprovalPolicy` with TTY-aware confirmation.
- Added progress abstraction: `RichProgressReporter`; CLI uses it.
- Added domain models: providers/agents mapping and selection, integrated into CLI.
- CLI wired to use `ChatSession` for tool flows and non-stream runs; streaming uses ChatSession helpers.
- Exit codes supported and tested: 0, 1, 4 (approval denied), 124 (timeout), 130 (interrupt).
- Test suite: unit tests cover adapters, session, domain models, and CLI commands.

### Remaining/Deferred

- Optional TUI layer scaffolding.
- Deeper MCP lifecycle/integration tests (spawn/retry/reconnect) and chaos tests.
- Additional CLI snapshot tests for complex outputs if needed.
