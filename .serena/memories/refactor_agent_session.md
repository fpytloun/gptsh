Status: Refactor complete and validated (55 tests passing)

Summary of key changes
- Ownership: Agent now owns a persistent ChatSession, which owns the LLMClient. No CLI-level session caches.
- Session API: ChatSession builds request params from llm._base and provided history only. No per-turn provider/agent config merges.
- Runner: run_turn/RunRequest simplified to only use Agent + ChatSession; progress default via NoOpProgressReporter.
- REPL relocation: Implemented under gptsh/cli/repl.py (core/repl.py removed). REPL reads/updates Agent.llm._base and uses Agent.session.
- Provider/agent config usage: provider_conf/agent_conf are only used during initialization via config_resolver. All subsequent changes go through Agent.llm._base and Agent.session.
- EntryPoint: run_llm attaches a persistent ChatSession to agent_obj.session for REPL and closes it on exit.
- Tests updated to match new signatures; build_prompt now takes (agent_name, model, readline_enabled), command_model mutates Agent directly.

Important references
- Agent dataclass: gptsh/core/agent.py:31-41
- ChatSession.from_agent: gptsh/core/session.py:101-117
- ChatSession._prepare_params: gptsh/core/session.py:148-181
- ChatSession.stream_turn: gptsh/core/session.py:188-450
- Runner.run_turn: gptsh/core/runner.py:151-288
- CLI REPL: gptsh/cli/repl.py
- CLI entrypoint run_llm: gptsh/cli/entrypoint.py:360-416

Follow-ups
- Docs updated next (AGENTS.md, README.md) to reflect REPL path and Agent→Session model.
- Ensure examples in examples/ use new REPL import path and Agent-based mutations.
- Keep provider_conf/agent_conf usage limited to config resolution.
