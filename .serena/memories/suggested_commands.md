# Suggested Commands (Ruff/Pytest/UV)

- Initialize dev env:
  - `UV_CACHE_DIR=.uv-cache uv pip install -e .[dev]`
- Lint & test:
  - `UV_CACHE_DIR=.uv-cache uv run ruff check`
  - `UV_CACHE_DIR=.uv-cache uv run pytest`
- CLI help:
  - `UV_CACHE_DIR=.uv-cache uv run gptsh --help`
- Optional auto-fix imports/format (review diff after):
  - `UV_CACHE_DIR=.uv-cache uv run ruff check --fix`
