### TODO List

- [ ] Create `gptsh/interfaces.py` defining `LLMClient`, `MCPClient`, `ApprovalPolicy`, `ProgressReporter`, and `UIInterface` protocols.  
- [ ] Implement `gptsh/llm/litellm_client.py` with `LiteLLMClient` and `_extract_text` logic.  
- [ ] Develop `gptsh/llm/tool_adapter.py` for MCP tool discovery and call parsing.  
- [ ] Extract `_MCPManager` into `gptsh/mcp/manager.py` and expose clean `MCPClient` API.  
- [ ] Introduce `gptsh/mcp/builtin/base.py` decorator registry and refactor existing builtin modules (`shell.py`, `time.py`).  
- [ ] Build `gptsh/core/approval.py` with `DefaultApprovalPolicy` (wildcard + interactive confirm).  
- [ ] Build `gptsh/core/progress.py` with `RichProgressReporter` for persistent, wrapped progress tasks.  
- [ ] Create `gptsh/domain/models.py` with typed dataclasses for config and runtime models.  
- [ ] Implement `ChatSession` in `gptsh/core/session.py` to orchestrate LLM calls, tool loops, approvals, and progress.  
- [ ] Create `gptsh/ui/cli.py` implementing `UIInterface`.  
- [ ] Update `cli/entrypoint.py` to instantiate and wire `LiteLLMClient`, `MCPManager`, `DefaultApprovalPolicy`, `RichProgressReporter`, and `ChatSession`.  
- [ ] Deprecate legacy functions (`complete_with_tools`, etc.) with thin wrappers for backward compatibility.  
- [ ] Add unit tests for all new components and their protocols.  
- [ ] Update `README.md` and code comments to explain the new modular architecture.  
- [ ] Plan and document a stepwise migration strategy that preserves CI passing at each stage.

## TODO / Implementation Status

### Completed
- [x] Scaffolded project structure and Python packages (`cli`, `config`, `core`, `mcp`, `plugins`, `utils`, `tests`)
- [x] Implemented config loader with YAML merging and environment variable expansion
- [x] Built CLI entry point with Click, supporting prompt, agent, model, stream, progress, debug flags
- [x] Integrated logging setup with configurable levels and formats
- [x] Added stdin handler with truncation strategy for piped input
- [x] MCP tool discovery and execution via official MCP SDK (stdio/HTTP/SSE)
- [x] Implemented asynchronous LLM calls via `litellm` with streaming and single-shot support
- [x] Wrote pytest tests for config loader, stdin handler, and CLI flows
- [x] Added --mcp-servers CLI option to override path to MCP servers file
- [x] Added --provider option to select LiteLLM provider from config
- [x] Added --list-providers CLI option to list configured providers
- [x] Rich Progress UI rendered to stderr; clean teardown before output
- [x] Output format flag `-o/--output` (text|markdown) with Markdown rendering at end
- [x] `--no-tools` to disable MCP; `--tools` to whitelist allowed MCP servers
- [x] Interactive REPL mode with colored "<agent>|<model>>" prompt
- [x] Maintain per-REPL session chat history (messages threaded through LLM calls)
- [x] Interactive mode accepts initial prompt from stdin or positional arg and continues after response
- [x] Persistent MCP sessions across the entire REPL; initialized once and cleaned up on exit; LiteLLM async client cleanup and warning suppression
- [x] REPL slash-commands: `/exit`, `/quit`, `/model <model>`, `/agent <agent>`, `/reasoning_effort [minimal|low|medium|high]`
- [x] Tab-completion for slash-commands and agent names; switching agent also updates model and reloads tools
- [x] Add progress bars and status feedback for long-running operations
- [x] Build interactive approval UX for destructive or privileged tool invocations
- [x] Support full agent presets (system/user prompts, model selection, tool policies)
- [x] Complete configuration overrides and merging for global and project-local settings

### Pending / Roadmap

#### MCP Integration
- [ ] Improve MCP lifecycle resilience (auto-respawn, backoff, health checks)
- [ ] Add option to define mcpServers in gptsh config per agents either by passing structure or including mcp_servers.json file

#### User Experience
- [ ] Detect TTY vs non-TTY and provide appropriate UI modes (spinner vs minimal output)
- [ ] Add session history, introduce new `-s [session]` and `--list-sessions` parameters

#### Configuration & Agents
- [ ] Implement log redaction for secrets and sensitive data
- [ ] Support logging into file

#### Workflows
- [ ] Add support for Workflows.

Workflow is using multiple steps that consists of agents and other execution blocks

Example config defining `create-git-commit` workflow that is using already defined `committer` agent and shell execution steps:

```yaml
workflow:
  create-git-commit:
    parameter:
      amend:
        # Will pass amend into workflow steps (shell variable of value 0/1 and into LLM step as user message)
        type: bool
      push:
        type: bool
    steps:
      - description: "Obtain diff of changes"
        on_failure: exit # Can be "exit" (default, will fail and pass output and exitcode) or "continue"
        shell:
          command:
            staged=$(git diff --cached)

            if [[ -z "$staged" && $amend -eq 0 ]]; then
              echo "No staged changes to commit!" >&2
              exit 1
            fi

            if [[ $amend -eq 1 ]]; then
              if [[ -z "$staged" ]]; then
                # No staged changes, just reword last commit
                git show --pretty=format:%B HEAD
              else
                # Staged changes and committed changes
                git show --pretty=format:'' HEAD
                echo "$staged"
              fi
            else
              echo "$staged"
            fi
      - description: "Generate commit message"
        llm:
          agent: committer
          prompt:
            user: |
              Generate single commit message based on provided diff:
              ${step[-1].stdout}
      - description: "Commit and push"
        shell:
          env:
            # define msg passed as env variable, reference last step's stdout.
            msg: "${step[-1].stdout}"
          command: |
            if [[ $amend -eq 1 ]]; then
              git commit --amend --edit -m "$msg"
            else
              git commit --edit -m "$msg"
            fi

            if [[ $push -eq 1 ]]; then
              upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u})
              remote=${upstream%%/*}
              branch=${upstream#*/}
              git push "$remote" HEAD:"$branch"
            fi
```

Usage will be very simple:
```sh
gptsh -w create-git-commit
# Or with parameters
gptsh -w create-git-commit --amend --push
```

#### Resilience & Timeouts
- [ ] Add request timeouts with exponential backoff and jitter for model and MCP calls

#### Packaging & Workflow
- [ ] Add `uv lock` workflow and packaging pipelines for releases
- [ ] Add more full tests coverage

#### Code quality & Optimization & Performance
- [ ] Cleanup code, deduplicate some parts (loading agents, etc.), use objects
