# gptsh

A modern, modular shell assistant powered by LLMs with first-class Model Context Protocol (MCP) support.

- Async-first core
- Configurable providers via LiteLLM (OpenAI, Claude, Perplexity, Azure, etc.)
- MCP tools discovery and invocation with resilient lifecycle
- Clean CLI UX with progress spinners and Markdown rendering

See AGENTS.md for development standards and architecture details.

## Goal

There's already many CLI tools for interaction with LLMs. Some of them are
designed for coding (eg. Aider, Opencode, Codex), some others are meant for
sysadmin or generic use (eg. shell-gpt).
Having a tool is no longer an issue, with LLMs almost anyone can vibe-code
anything they like, even people without prior experience. We can argue about
quality and security of resulting products but the fact is that over time, as
LLMs are getting rapidly better as well as people are finding new approaches,
it will become irrelevant.

As this world evolves quickly, it is clear that it is not about tools; it is
about **human creativity**, ideas, and building a modular architecture using
blocks that can be replaced at any time.

gptsh aims to be a versatile, simple, and extensible tool built around the idea of
agents, where an agent is an LLM with a role-specific prompt that defines its behavior
and an assigned set of tools (using MCP).

It is meant to be simple—mostly plug-and-play—with examples and proven setups
and usage patterns shared by others.

You can easily use it with a single/default agent and a Claude-like
`mcp_servers.json` as-is.

Or you can define multiple agents with different roles and tools and use them as
needed.

Or you can set up a more complex environment with multiple agents (e.g.,
Software Developer, QA Engineer) and one agent (Manager) that receives the user
prompt, orchestrates work, and delegates tasks to these agents. Even an agent can be
invoked as a tool from another agent.

```mermaid
flowchart TD
  %% User interaction
  U[User Prompt] --> M[Manager Agent]

  %% Manager orchestrates tasks
  M -->|Delegates task| Dev[Software Developer Agent]
  M -->|Delegates task| QA[QA Engineer Agent]

  %% Agents can call other agents as tools
  Dev -.->|Calls as tool| QA
  QA  -.->|Calls as tool| Dev
  M   -.->|Calls as tool| Dev
  M   -.->|Calls as tool| QA

  %% Agents use LLM via LiteLLM
  subgraph LLM_Stack[LLM Access]
    direction TB
    Lite[LiteLLM Provider Router]
    Model[(LLM Model)]
    Lite --> Model
  end

  Dev -->|Chat / Tool selection| Lite
  QA  -->|Chat / Tool selection| Lite
  M   -->|Coordination / Planning| Lite

  %% MCP Servers and Tools
  subgraph MCP[MCP Tooling Layer]
    direction TB
    subgraph Builtins[Builtin Tools]
      T1[[time]]
      T2[[shell]]
    end
    subgraph Ext[External MCP Servers]
      FS[[filesystem]]
      GIT[[git]]
      WEB[[tavily/web]]
      OTHER[[...]]
    end
  end

  Dev -->|Invoke tool| MCP
  QA  -->|Invoke tool| MCP
  M   -->|Invoke tool| MCP

  %% Results aggregation
  Dev -->|Work output| M
  QA  -->|Test results / feedback| M
  M -->|Aggregated answer| U
```

## Installation

We use uv/uvx for environment management and running:

### Package

```bash
uvx --from gptsh-cli gptsh --help
```

You can also set alias:
```sh
alias gptsh="uvx --from gptsh-cli gptsh"
```

### Git checkout

```bash
uv venv
uv pip install -e .[dev]
```

Run:

```bash
uv run gptsh --help
```

## Quick Start

Single-shot prompt:

```bash
gptsh "Summarize the latest project changes"
```

Pipe input from stdin:

```bash
git diff | gptsh "Explain the changes and suggest a commit message"
```

Plain text output (default is markdown):

```bash
gptsh -o text --no-progress "Generate shell command to rename all files in directory and prefix them with xxx_"
```

## CLI Usage

```text
Usage: gptsh [OPTIONS] [PROMPT] COMMAND [ARGS]...

  gptsh: Modular shell/LLM agent client.

Options:
  --provider TEXT               Override LiteLLM provider from config
  --model TEXT                  Override LLM model
  --agent TEXT                  Named agent preset from config
  --config TEXT                 Specify alternate config path
  --stream / --no-stream
  --progress / --no-progress
  --debug
  -v, --verbose                 Enable verbose logging (INFO)
  --mcp-servers TEXT            Override path to MCP servers file
  --list-tools
  --list-providers              List configured providers
  -o, --output [text|markdown]  Output format
  --no-tools                    Disable MCP tools (discovery and execution)
  --tools TEXT                  Comma/space-separated MCP server labels to
                                allow (others skipped)
  -h, --help                    Show this message and exit.
```

## MCP Tools

List available tools discovered from configured MCP servers:

```bash
gptsh --list-tools
```

Disable tools entirely:

```bash
gptsh --no-tools "Explain which tools are available to you"
```

Allow only specific MCP servers (whitelist):

```bash
gptsh --tools serena --list-tools
gptsh --tools serena "Only Serena tools will be available"
```

This flag supports multiple labels with comma/space separation:

```bash
uvx gptsh --tools serena,tavily
```

## Configuration

Config is merged from:
1) Global: ~/.config/gptsh/config.yml
2) Global snippets: ~/.config/gptsh/config.d/*.yml (merged in lexicographic order)
3) Project: ./.gptsh/config.yml

Environment variables may be referenced using ${VAR_NAME} (and ${env:VAR_NAME} in mcp_servers.json is normalized to ${VAR_NAME}). YAML also supports a custom !include tag resolved relative to the including file, with wildcard support. For example:
- agents: !include agents.yml
- agents: !include agents/*

### MCP

MCP servers configuration is taken from these locations. As most models has limit for number of tools available, only one servers config is loaded in this order:
1) CLI parameter (eg. `--mcp-servers mcp_servers.json`)
2) Current directory (`./.gptsh/mcp_servers.json`)
3) Global (`~/.config/gptsh/mcp_servers.json`)

There are also builtin MCP servers that are always available (`time`, `shell`) but can be referenced and disabled with `mcp_servers.json`.

### Example config.yml

```yaml
default_agent: default
default_provider: openai

providers:
  openai:
    model: openai/gpt-4.1

agents:
  default:
    model: gpt-4.1
    output: markdown
    autoApprove: ["time"]
    prompt:
      system: "You are a helpful assistant called gptsh."
  cli:
    output: text
    model: "gpt-4.1-mini"
    tools: ["shell"]
    prompt:
      system: |
        You are expert system administrator with deep knowledge of Linux and Unix-based systems.
        You have in two modes: either you execute command using tool (default if tool is available) or you provide command that user can execute.

        **Instructions (tool):**
        - If you have shell execution tool available, call that tool to execute command by yourself
        - After command is completed, check exit code and return tool output
        - Return only tool output as it would return if executed directly
        - Do not make up output of tool and pretend you executed something!

        **Instructions (without tool):**
        - Return command that can be executed as is on given system and does what user wants
        - Make sure your output is compatible with POSIX-compliant shell
        - Return only ready to be executed command and nothing else!
        - It is likely to be passed to sh/bash via stdin
        - If command is destructive, make sure to use echo/read for user confirmation unless user commands to skip confirmation
  hello:
    tools: []
    prompt:
      user: "Hello, are you here?"
```

For full example, see `examples` directory.

### Example MCP servers file (mcp_servers.json)

```json
{
  "mcpServers": {
    "sequentialthinking": {
      "args": [
        "run",
        "--rm",
        "-i",
        "mcp/sequentialthinking"
      ],
      "autoApprove": [
        "sequentialthinking"
      ],
      "command": "docker"
    },
    "filesystem": {
      "args": [
        "run",
        "-i",
        "--rm",
        "--mount",
        "type=bind,src=${HOME},dst=${HOME}",
        "mcp/filesystem",
        "${HOME}"
      ],
      "autoApprove": [
        "directory_tree",
        "get_file_info",
        "list_allowed_directories",
        "list_directory",
        "read_file",
        "read_multiple_files",
        "search_files"
      ],
      "command": "docker"
    },
    "git": {
      "args": [
        "mcp-server-git"
      ],
      "autoApprove": [
        "git_diff",
        "git_diff_staged",
        "git_diff_unstaged",
        "git_log",
        "git_show",
        "git_status",
        "git_branch"
      ],
      "command": "uvx"
    },
    "tavily": {
      "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "TAVILY_API_KEY",
        "mcp/tavily"
      ],
      "autoApprove": [
        "tavily-search",
        "tavily-extract",
        "tavily-crawl",
        "tavily-map"
      ],
      "command": "docker",
      "env": {
        "TAVILY_API_KEY": "${TAVILY_API_KEY}"
      }
    }
  }
}
```

- Use ${VAR} for env expansion.
- autoApprove lists tools that should be pre-approved by the UI.

You can override servers files with the CLI:

```bash
gptsh --mcp-servers ./.gptsh/mcp_servers.json --list-tools
```

You can restrict which servers load by using:

```bash
gptsh --tools serena "Only serena’s tools are available"
```

## Examples

Ask with project context piped in:

```bash
rg -n "async def" -S | gptsh "What async entry points exist and what do they do?"
```

Use Text output for plain logs:

```bash
gptsh -o text "Return a one-line status summary"
```

Use a different provider/model:

```bash
gptsh --provider openai --model gpt-4o-mini "Explain MCP in a paragraph"
```

Disable progress UI:

```bash
gptsh --no-progress "Describe current repo structure"
```

## Exit Codes

- 0   success
- 1   generic failure
- 2   configuration error (invalid/missing)
- 3   MCP connection/spawn failure (after retries)
- 4   tool approval denied
- 124 operation timeout
- 130 interrupted (Ctrl-C)

## Development

Run tests:

```bash
uvx pytest -q
```

Project scripts:

- Entry point: gptsh = "gptsh.cli.entrypoint:main"
- Keep code async; don’t log secrets; prefer uv/uvx for all dev commands.

## Troubleshooting

- No tools found: check --mcp-servers path, server definitions, and network access.
- Stuck spinner: use --no-progress to disable UI or run with --debug for logs.
- Markdown output looks odd: try -o text to inspect raw content.

---
Feedback and contributions are welcome!
