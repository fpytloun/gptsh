# Feature: Session History (Persisted Conversations)

This memory captures the complete design and implementation plan to add persisted session history for gptsh, covering CLI semantics, storage, schema, APIs, integration points, title generation via a small model, tests, and docs.

## Summary

- Persist every conversation to disk as a session (works for non‑interactive and REPL).
- Users can reference sessions by a simple short id or by index (0 = newest).
- Provide `-s/--session` to resume/append and `--list-sessions` to enumerate.
- Store sessions as JSON files under XDG state dir, one file per session.
- Generate a short, human-friendly title exactly once from the first user input using a small model (agent/provider `model_small`).

## Goals

- Make sessions straightforward to save, list, and resume.
- Avoid storing secrets or provider credentials.
- Keep IDs short, filenames sortable, and resolution flexible (index / id / unique prefix).
- Minimize invasive changes: new core module for persistence, small touches in CLI paths.

## Non‑Goals

- No binary serialization (eg. pickle) or storing raw client objects.
- No multi-file session store (simple one JSON file per session).
- No server-side sync yet.

## CLI

- `-s, --session SESSION_REF`
  - If provided and found: load that session, preload history, and append new turns.
  - If omitted: after the first turn, create a new session and save.
  - SESSION_REF resolution order:
    1) Integer index (newest-first): `0` newest, `1` previous, etc.
    2) Exact short id match (e.g., `7x2m`).
    3) Unique id prefix match (e.g., `7x`). Ambiguous prefix → error with choices.
- `--list-sessions`
  - Print newest-first lines formatted: `[index] id DATETIME title (agent|model)`
  - DATETIME shown in local time (but UTC timestamps are stored in JSON).

## Indexing Semantics

- Newest-first zero-based indexing: `0` = latest, `1` = one before latest, etc.
- Aligns with common shell history patterns and avoids minus signs.

## IDs and Filenames

- Short ID: 4-char base36 (e.g., `7x2m`), collision-checked across existing files.
- Filename: `<UTC yyyyMMdd-HHmmss>-<id>.json` (e.g., `20251027-091530-7x2m.json`).
- This yields lexicographically sortable filenames while allowing a compact id users can type.

## Storage Location

- Directory: `${XDG_STATE_HOME:-~/.local/state}/gptsh/sessions`.
- Create parent dirs on first use.

## JSON Schema (one file per session)

```jsonc
{
  "id": "7x2m",
  "title": "Feeling Fine Check",
  "created_at": "2025-10-27T09:15:30Z",
  "updated_at": "2025-10-27T09:16:04Z",
  "agent": {
    "name": "default",
    "model": "gpt-4.1",
    "model_small": "gpt-4.1-mini",
    "params": {
      "temperature": 0.7,
      "reasoning_effort": "low"
    }
  },
  "provider": { "name": "openai" },
  "messages": [
    { "role": "user", "content": "I am feeling fine" },
    { "role": "assistant", "content": "That's great to hear!" },
    { "role": "user", "content": "How am I feeling?" },
    { "role": "assistant", "content": "You said you're feeling fine." }
  ],
  "usage": {
    "tokens": {
      "prompt": 56,
      "completion": 12,
      "total": 68,
      "reasoning_tokens": null,
      "cached_tokens": null
    },
    "cost": 0.00043
  },
  "meta": {
    "output": "markdown",
    "mcp_allowed_servers": ["time", "shell"]
  }
}
```

Notes:
- `messages` use the OpenAI-style message schema; tool messages (role="tool") and assistant messages containing `tool_calls` are preserved when present.
- `params` only include safe generation params (no secrets or headers).
- `provider.name` is stored for reference but not its secrets.

## Title Generation

- Trigger: Only once, on the first save of a brand-new session that lacks a title.
- Source: Only the first user message (ignore assistant or tools).
- Model selection: `agent.model_small` > `provider.model_small` > fallback to main model.
- Prompting:
  - System: “You generate a short, human-friendly title for a conversation based solely on the first user message. Return 3–7 plain words in Title Case. No punctuation, no quotes, no extra text.”
  - User: `<first user message>`
- Parameters: `{ temperature: 0.2, max_tokens: 24 }`, tools disabled; use existing provider via LiteLLM and override `model` for this one-shot call.
- If generation fails, leave title empty; do not retry automatically.

## New Module: gptsh/core/sessions.py

Functions:

- `get_sessions_dir(config: dict | None = None) -> Path`
  - Compute and create the sessions directory.

- `list_sessions(limit: int | None = None) -> list[SessionSummary]`
  - Read directory, parse filename timestamps + ids, load minimal metadata from JSON (title, agent, updated_at). Return newest-first summaries with index implied by order.

- `resolve_session_ref(ref: str) -> str`
  - If `ref.isdigit()` → interpret as index; map to id via newest-first ordered list.
  - Else treat as id/prefix; resolve to a single id. If ambiguous → raise ValueError with candidates.

- `load_session(id_or_filename: str) -> dict`
  - Accept id or full filename. Find file, read JSON, validate required keys.

- `save_session(doc: dict) -> str`
  - If `doc` has no `id`, allocate (generate unique 4-char base36). If new, create filename with current UTC ts; else locate its existing file by id. Update `updated_at`. Write pretty-compact JSON (ensure_ascii=false).
  - Return filepath string.

- `new_session_doc(agent_info: dict, provider_info: dict, *, output: str | None, mcp_allowed_servers: list[str] | None) -> dict`
  - Return an initialized doc with id set, timestamps, empty title/messages/usage, and metadata.

- `append_messages(doc: dict, new_messages: list[dict], usage_delta: dict | None) -> None`
  - Extend `messages`. Merge `usage_delta` into `doc["usage"]` by summing cost and replacing latest tokens if provided.

- `generate_title(first_user: str, *, small_model: str | None, llm: LLMClient) -> str | None`
  - Run a tiny completion as described above. Return stripped single-line title or None.

Supporting types:
- `SessionSummary = TypedDict("SessionSummary", {"id": str, "filename": str, "created_at": str, "updated_at": str, "title": str | None, "agent": str | None, "model": str | None})`

Implementation notes:
- Use `pathlib`, `datetime.timezone.utc`, and `json`.
- Base36 id: `random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=4)`; loop until unique (collisions are extremely unlikely but handled).
- Store timestamps in ISO 8601 with `Z`.
- Localize to user local time only when printing in `--list-sessions`.

## Integration Points

### Non‑interactive (gptsh/cli/entrypoint.py)

- Add options:
  - `@click.option("--session", "-s", "session_ref", default=None)`
  - `@click.option("--list-sessions", "list_sessions_flag", is_flag=True, default=False)`

- Listing branch:
  - If `list_sessions_flag`: call `list_sessions()`; print lines as `[index] id DATETIME title (agent|model)` using newest-first order; then exit 0.

- Running branch:
  - Determine output format and flags as today.
  - If `session_ref` provided:
    - Resolve to id → load doc → preload a ChatSession:
      - Build or reuse `agent_obj.session`; set `session_obj.history = doc["messages"]` before `run_turn_with_request`.
      - Pass `messages_sink=[]` into run to capture only new deltas.
    - After run:
      - Append `messages_sink` to `doc` with `append_messages` and merge usage from `agent_obj.session.usage`.
      - Save with `save_session(doc)` (do not regenerate title).
  - Else (no `session_ref`):
    - After run, build `doc = new_session_doc(...)` with agent/provider info:
      - agent.name, main model, model_small if any, and safe params.
      - provider name.
      - output and allowed servers if available (optional).
    - Compute first user message from the messages sink or prompt; if present, run `generate_title` and set `doc["title"]`.
    - Append `messages_sink` and usage; save.

- Do not print “saved session id” unless later desired; stay silent.

### REPL (gptsh/cli/repl.py)

- Accept a `session_ref` parameter in `run_agent_repl`/`run_agent_repl_async` and plumb from entrypoint when `-i` is used.
- If `session_ref` provided:
  - Resolve → load doc → set `agent.session.history = doc["messages"]` prior to entering loop.
- After each successful `_run_once`, compute new messages since last length and persist:
  - Call `append_messages(doc, new_messages, usage_delta=session.usage)` and `save_session(doc)`.
- On REPL exit:
  - If the session was newly created in this run and `title` missing but there is at least one user message, generate title and save.

## Config: model_small

- Support `model_small` at:
  - `agents.<name>.model_small`
  - `providers.<name>.model_small`
- Resolution helper (can be local to CLI):
  - `resolve_small_model(agent_conf, provider_conf) -> Optional[str]` with precedence agent > provider > None.
- Title generation overrides only the request `model` param for that single call.

## Printing (list)

- Format: `[index] id yyyy-MM-dd HH:mm title (agent|model)`
- Use local time for readability.

## Error Handling

- Unknown `SESSION_REF`: exit with code 2 and message.
- Ambiguous id prefix: error with list of matching ids.
- JSON read errors: skip corrupt entries in listing; error on load.
- IO failures: surface a clear message and exit 1/2 accordingly.

## Security & Privacy

- Never store API keys, tokens, headers, or request bodies with secrets.
- Only store safe generation params (temperature, reasoning_effort); not `extra_headers`.
- Do not log secrets in CLI output.

## Tests

Add unit tests under `gptsh/tests/`:

- `test_sessions_id_and_filename`
  - Generate ids, ensure base36 4 chars; filename pattern; uniqueness in temp dir.

- `test_sessions_save_and_load`
  - Create new doc, append messages, save, load back; check fields and timestamps.

- `test_sessions_list_and_index`
  - Create multiple session files with varying timestamps; ensure newest-first order; index mapping 0..N; listing lines formatting.

- `test_sessions_resolve_ref`
  - Resolve numeric indices, exact id, unique prefix; ambiguous prefix should error.

- `test_noninteractive_with_session`
  - Run CLI once to create; run again with `-s 0`; ensure messages appended and title unchanged.

- `test_repl_preload_and_append` (can simulate minimal loop or call run_llm with exit_on_interrupt=False)
  - Preload history and save on each turn.

- `test_title_generation_small_model_resolution`
  - Ensure agent > provider > fallback; ensure system prompt constraints (no punctuation) roughly respected.

## Implementation Steps

1) Create `gptsh/core/sessions.py` with the APIs listed.
2) Add CLI flags and listing flow to `gptsh/cli/entrypoint.py`.
3) Wire non‑interactive execution to load/append/save via messages_sink and session.usage.
4) Update `gptsh/cli/repl.py` to accept `session_ref`, preload history, and persist after each turn; finalize title at REPL startup (new sessions only) if first user exists and title missing, or on first save.
5) Add `model_small` parsing (no strict typing required initially); implement small-model resolution helper where needed.
6) Add tests.
7) Update README.md and AGENTS.md with usage, paths, and examples (e.g., `gptsh -s 0 "Continue"`, `gptsh --list-sessions`).

## Pseudocode Highlights

- Non‑interactive run (simplified):

```python
messages_sink = []
req = RunRequest(..., session=preloaded_session_or_none, messages_sink=messages_sink)
await run_turn_with_request(req)

if session_ref:
    doc = load_session(id)
else:
    doc = new_session_doc(agent_info, provider_info, output=output_effective, mcp_allowed_servers=...)
    first_user = extract_first_user_from(messages_sink, fallback=prompt)
    if first_user and not doc.get("title"):
        small = resolve_small_model(agent_conf, provider_conf) or agent.llm._base.get("model")
        title = generate_title(first_user, small_model=small, llm=agent.llm)
        if title:
            doc["title"] = title
append_messages(doc, messages_sink, usage_delta=session.usage)
save_session(doc)
```

- Listing:

```python
for idx, s in enumerate(list_sessions()):
    dt_local = to_local(s["updated_at"]) or to_local(s["created_at"]) 
    print(f"[{idx}] {s['id']} {fmt(dt_local)} {s.get('title','(untitled)')} ({s.get('agent','?')}|{s.get('model','?')})")
```

## Edge Cases

- Title generation skipped: if first user message missing or empty.
- Tool-only exchanges preserved in messages; listing still works.
- Very long first input: small max_tokens yields short title regardless.

## Future Extensions

- Add filters to `--list-sessions` (e.g., by agent name) and a `--delete-session` command.
- Provide `--export-session ID` to write a markdown transcript.
- Allow per-session metadata like tags.

## Acceptance Criteria

- Users can create a session implicitly, list sessions, and resume by `-s` with either a numeric index or the short id.
- Titles are concise and generated only once from the first user message.
- No secrets stored; files live under XDG state dir.
- Tests cover id gen, listing/indexing, ref resolution, non‑interactive and REPL flows.
