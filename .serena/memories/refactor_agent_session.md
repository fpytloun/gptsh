# Implementation Plan: Refactor Agent to own ChatSession and LLM; stop per-turn config merging; move REPL to CLI

Purpose
- What: Refactor runtime ownership so a single Agent instance holds a persistent ChatSession (MCP + LLM client), and all turns reuse that session. Stop merging provider_conf/agent_conf on every turn. Move REPL module under gptsh/cli.
- Why:
  - Eliminate per-turn overrides (e.g., None values in provider_conf accidentally overriding llm._base).
  - Establish one source of truth (Agent/session/LLM base params) that REPL commands can safely mutate.
  - Improve performance and stability by reusing MCP connections and shared HTTP sessions across the app lifetime.
  - Improve maintainability of code by removing duplicated logic
  - Clarify layering: REPL is a CLI concern.

Current state (summary)
- Agent is constructed from merged config and resolves tools/policy/llm, but ChatSession and per-turn params are built in runner using provider_conf and agent_conf.
- ChatSession._prepare_params merges provider_conf and agent_conf into params each turn, then LiteLLMClient merges {**llm._base, **params}. This allows None from provider_conf to override llm._base values, causing REPL changes (e.g., /reasoning_effort) to be ineffective.
- REPL lives in gptsh/core/repl.py and partially manipulates both provider_conf_local and agent.llm._base to reflect live changes.

Target architecture
- Agent
  - Holds a single ChatSession instance at agent.session.
  - Remains the single source of truth for model/params (through agent.session.llm._base) and tools/policy.
  - Provides small mutation helpers or a clear convention to update live params (e.g., agent.session.llm._base["model"] = ...).
- ChatSession
  - Holds LLMClient instance (agent.session.llm) and MCP manager/state.
  - Prepares per-turn params exclusively from llm._base plus session/tool fields; does not merge external provider_conf/agent_conf per turn.
  - Maintains usage accounting and exposes it for REPL (/info).
- LLMClient (LiteLLMClient)
  - Already includes a shared aiohttp.ClientSession and debug logs; reused for the entire Agent’s life.
- Runner
  - Receives only Agent (with session) and turn options (prompt, history, streaming flags).
  - No longer depends on provider_conf/agent_conf to build per-turn params; if arguments remain for backward-compat, ignore them.
- REPL
  - Move module to gptsh/cli/repl.py.
  - Operates only on Agent: /model and /reasoning_effort mutate agent.session.llm._base; /info inspects agent.session.usage and agent.session.llm._base.
  - Session reuse is guaranteed; closing happens at REPL exit via entrypoint cleanup.

Implementation plan

A) Data ownership refactor
1) Agent to own ChatSession
- Add agent.session: ChatSession constructed once during agent build (core/config_resolver.build_agent).
- Construct ChatSession with:
  - llm from resolved provider/model params (stored in llm._base).
  - policy and tools (resolved once).
  - mcp manager per agent (start on first use or immediately).
- Remove any per-turn provider_conf/agent_conf dependency from session logic.

2) ChatSession param preparation
- In ChatSession._prepare_params:
  - Build params from llm._base only (model + generation params).
  - Add messages (system from agent prompt, history, user).
  - Add tools/tool_choice/parallel_tool_calls when enabled.
  - Do not merge provider_conf or agent_conf each turn.
- Keep existing non-stream fallback for tool calls; retain usage updates and debug logs.

3) Runner decoupling
- Update runner.run_turn signature usage to ignore provider_conf/agent_conf args (keep them for backward compatibility if required by tests, but do not use them).
- Fetch everything from agent.session (session reuse if provided, else create once from agent.session).
- Ensure session.start() is called when needed and closed only when runner created a temporary session (non-REPL); REPL-owned session remains open until REPL exit cleanup.

4) REPL move and adaptation
- Relocate gptsh/core/repl.py to gptsh/cli/repl.py.
- Adjust imports (entrypoint should import from gptsh.cli.repl).
- REPL commands:
  - /model: update agent.session.llm._base["model"]; refresh prompt string; no provider_conf_local overrides.
  - /reasoning_effort: update agent.session.llm._base["reasoning_effort"]; do not rely on provider_conf override; optionally also set agent.session.llm._base["reasoning"] = {"effort": "..."} for better provider coverage.
  - /info: use litellm.get_max_tokens or litellm.utils._get_model_info_helper to get max context; show usage from agent.session.usage; show params from agent.session.llm._base.
  - /agent: switching agents should construct a new Agent (thus new session). Ensure old session gets closed or kept until exit per policy.
  - /no-tools: rebuild Agent or refresh tool specs on the same session according to policy (MCP allowed servers), or recreate session if that’s simpler.

5) Lifecycle and cleanup
- Entry point after REPL exits: close any cached Agent sessions (aclose), which in turn closes MCP and the shared aiohttp session.
- Non-REPL one-shot: runner’s finally calls session.aclose() if it created a temporary session.

B) Backward-compat and migration
- Keep runner.run_turn arguments (provider_conf/agent_conf) but ignore them; update tests to not rely on those dicts affecting per-turn behavior.
- Confirm CLI listings and agent resolution still work (list tools, list agents).
- Update /info’s displayed model: ensure it reads from agent.session.llm._base["model"], which reflects any live changes.

C) Testing plan
- Unit tests:
  - Runner: verify a no-tools turn runs exactly one stream call; verify result_sink populated; ensure ignoring provider_conf/agent_conf does not break existing tests.
  - REPL: mock Agent and verify /model and /reasoning_effort update agent.session.llm._base and reflect in subsequent calls (can assert via debug logs or direct inspection).
  - Session reuse: create Agent once, run two turns; assert the same shared_session id is logged for both turns.
  - Non-stream fallback: simulate streamed deltas with no concrete tool_calls; assert fallback triggers once and usage is updated from non-stream response.
- Integration-ish:
  - Ensure REPL /info prints model from agent.session.llm._base, parameters, and usage after a couple of turns.
  - Confirm MCP manager persists across turns and closes at REPL exit.

D) Logging and observability
- Keep debug logs in LiteLLMClient (complete/stream) for model, message count, and shared_session id.
- Add optional debug logs in session when fallback is triggered (already added).
- Ensure no stdout prints in session logic; use logging.

E) Performance and correctness considerations
- Shared HTTP session reuse improves prompt caching potential and reduces TLS/handshake overhead.
- Eliminating per-turn dict merges avoids accidental None overrides and unnecessary CPU work.
- MCP sessions remain alive across turns; ensure reconnection/respawn logic remains unaffected.
- Reasoning effort mapping: some providers require nested reasoning: {effort: …}. Consider mirroring reasoning_effort into reasoning when set (minimal cross-provider coverage), but keep configurable.

F) CLI and package layout
- Move REPL to gptsh/cli/repl.py.
- Update entrypoint imports accordingly; leave the public API unchanged.
- Confirm user CLI UX remains the same.

G) Security and approvals
- No changes to approval flows; rely on existing session approval policy.
- Ensure secrets are not logged; headers remain unprinted.

H) Step-by-step delivery
1) Implement Agent.session and construct ChatSession/LLM inside build_agent.
2) Update ChatSession._prepare_params to only use llm._base (remove provider_conf/agent_conf merging).
3) Update runner to use only Agent/session.
4) Move REPL to gptsh/cli; adapt commands to mutate Agent/session.llm._base; update /info.
5) Adjust entrypoint cleanup to close Agent sessions at exit.
6) Update tests; remove assumptions about per-turn provider_conf/agent_conf.
7) Run:
   - UV_CACHE_DIR=.uv-cache uv run ruff check
   - UV_CACHE_DIR=.uv-cache uv run pytest

Constraints and notes
- Keep code async-first; don’t block the loop.
- Don’t log secrets or headers.
- Maintain compatibility for CLI listing flags and exit codes.
- Avoid changing public function names unless necessary; favor internal reshaping.

Acceptance criteria
- REPL /reasoning_effort and /model changes persist across turns without being overridden.
- No per-turn provider_conf/agent_conf merging; Agent.session.llm._base is the authoritative set of params.
- Tests: all pass after updates; especially runner tests and CLI entrypoint tests.
- REPL moved under gptsh/cli; entrypoint imports updated; no regressions in CLI behavior.

Next step
- Do you want me to start with step 1 (Agent.session + construct ChatSession in build_agent) and wire runner to use only Agent/session?
