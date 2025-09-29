# gptsh

A modern, modular shell assistant powered by LLMs with first-class Model Context Protocol (MCP) support.

- Async-first core
- Configurable providers via LiteLLM (OpenAI, Claude, Perplexity, Azure, etc.)
- MCP tools discovery and invocation with resilient lifecycle
- Clean CLI UX with progress spinners and Markdown rendering

See AGENTS.md for development standards and architecture details.

## Installation

We use uv/uvx for environment management and running:

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
uvx gptsh "Summarize the latest project changes"
```

Pipe input from stdin:

```bash
git diff | uvx gptsh "Explain the changes and suggest a commit message"
```

Plain text output (default is markdown):

```bash
uvx gptsh -o text --no-progress "Generate shell command to rename all files in directory and prefix them with xxx_"
```

Markdown rendering (default): the model’s output is rendered at the end as rich Markdown.

Text mode (prints as it streams):

```bash
uvx gptsh -o text "Return a single-line answer"
```

## CLI Usage

```text
gptsh [OPTIONS] [PROMPT]

Options:
  --provider TEXT               Override LiteLLM provider from config
  --model TEXT                  Override LLM model
  --agent TEXT                  Named agent preset from config (default: default)
  --config PATH                 Specify alternate config path
  --stream / --no-stream        Enable/disable streaming (default: on)
  --progress / --no-progress    Show/hide progress UI (default: on)
  --debug                       Enable debug logging (DEBUG)
  -v, --verbose                 Enable verbose logging (INFO)
  --mcp-servers TEXT            Override path(s) to MCP servers file(s) (comma or space-separated)
  --list-tools                  List discovered MCP tools by server and exit
  --list-providers              List configured providers and exit
  -o, --output [text|markdown]  Output format (default: markdown)
  --no-tools                    Disable MCP tools (discovery and execution)
  --tools TEXT                  Comma/space-separated MCP server labels to allow (others skipped)
  -h, --help                    Show this message and exit
```

Notes:
- Progress spinners are rendered to stderr and disappear before any stdout content.
- In Markdown mode, streaming chunks are buffered and rendered at the end as Markdown.
- In Text mode, chunks are printed live; a trailing newline is added only if the final chunk didn’t include one.

## MCP Tools

List available tools discovered from configured MCP servers:

```bash
uvx gptsh --list-tools
```

Disable tools entirely:

```bash
uvx gptsh --no-tools "Explain why tools are disabled"
```

Allow only specific MCP servers (whitelist):

```bash
uvx gptsh --tools serena --list-tools
uvx gptsh --tools serena "Only Serena tools will be available"
```

This flag supports multiple labels with comma/space separation:

```bash
uvx gptsh --tools "serena tavily"
```

## Configuration

Config is merged from:
1) Global: ~/.config/gptsh/config.yml
2) Project: ./.gptsh/config.yml

Environment variables may be referenced using ${VAR_NAME} (and ${env:VAR_NAME} in mcp_servers.json is normalized to ${VAR_NAME}).

### Example config.yml

```yaml
agent: default

progress: true
timeouts:
  request_seconds: 60

stdin:
  max_bytes: 5242880
  overflow_strategy: summarize  # summarize | truncate

logging:
  level: info
  format: text   # text | json
  redact_keys: ["api_key", "authorization"]

# Providers are LiteLLM-compatible; provider key is used with --provider
providers:
  openai:
    model: gpt-4o-mini
    api_key_env: OPENAI_API_KEY
    base_url: null
    extra_headers: {}
    # Optional MCP overrides (merged with global MCP config)
    mcp:
      tool_choice: auto

agents:
  default:
    model: gpt-4o-mini
    prompt:
      system: "You are a helpful assistant called gptsh."
      # Optional immediate user prompt:
      # user: "Print hello world"
```

### Example MCP servers file (mcp_servers.json)

```json
{
  "mcpServers": {
    "tavily": {
      "transport": { "type": "sse", "url": "https://api.tavily.com/mcp" },
      "credentials": { "headers": { "Authorization": "Bearer ${TAVILY_API_KEY}" } },
      "autoApprove": ["tavily_search"]
    },
    "serena": {
      "transport": { "type": "stdio" },
      "command": "uvx",
      "args": ["serena-mcp"],
      "env": {}
    }
  }
}
```

- Use ${VAR} for env expansion.
- autoApprove lists tools that should be pre-approved by the UI.

You can override servers files with the CLI:

```bash
uvx gptsh --mcp-servers ./.gptsh/mcp_servers.json --list-tools
```

You can restrict which servers load by using:

```bash
uvx gptsh --tools serena "Only serena’s tools are available"
```

## Examples

Ask with project context piped in:

```bash
rg -n "async def" -S | uvx gptsh "What async entry points exist and what do they do?"
```

Use Text output for plain logs:

```bash
uvx gptsh -o text "Return a one-line status summary"
```

Use a different provider/model:

```bash
uvx gptsh --provider openai --model gpt-4o-mini "Explain MCP in a paragraph"
```

Disable progress UI:

```bash
uvx gptsh --no-progress "Describe current repo structure"
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
