# Project Overview

- Purpose: A modular Python CLI (gptsh) for conversational, tool-augmented LLM workflows using LiteLLM and MCP (Model Context Protocol). Provides REPL mode, streaming, and resilient MCP lifecycle with local/remote servers. 
- Tech stack: Python 3.10+, click (CLI), litellm, mcp (Python SDK), pyyaml, rich, httpx, python-dotenv; pytest for tests; uv/uvx for env + commands.
- Entrypoint: `gptsh.cli.entrypoint:main` exposed as `gptsh` console script.
- Key features: async everywhere; config merging (global/main + snippets + project local) with env var expansion and custom `!include`; MCP discovery, respawn, reconnect; approvals; progress UI; stdin handling; agents/presets; security-first logging.
- Structure: `gptsh/cli` (CLI), `gptsh/config` (loader, merging), `gptsh/core` (stdin/logging), `gptsh/llm` (session + LiteLLM params and tool loop), `gptsh/mcp` (clients + builtin servers), `gptsh/tests` (pytest), examples, `.gptsh/` (sample mcp_servers.json).