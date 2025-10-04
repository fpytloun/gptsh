# Task Completion Checklist

- Run unit tests: `uv run pytest -q` and ensure all pass.
- If CLI behavior changed, validate `--help` output and key flows:
  - `--list-tools`, `--tools`, `--no-tools`, `--mcp-servers`
  - Single-shot prompt; streaming and non-streaming; stdin piping.
- Verify MCP discovery:
  - With default `.gptsh/mcp_servers.json` present: `uv run gptsh --list-tools`
  - With filtering: `uv run gptsh --tools serena --list-tools`
- Check logging level and output format switches (`--debug`, `-v`, `-o text|markdown`).
- Ensure no secrets logged; review diffs/params.
- Adhere to async style and typing; avoid blocking operations.
- Update README or examples if behavior changed.
