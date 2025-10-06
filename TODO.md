# TODO

## Important
- [ ] System prompt override for agents does not seem to work
- [x] Fail if attempting to use agent that is not defined
- [x] Fail if instructing to use config (-c) that does not exist
- [x] Fail if instructing to use mcp_servers (--mcp-servers) file that does not exist
- [ ] Fail if mcp.servers is string but not valid JSON

## MCP Integration
- [ ] Improve MCP lifecycle resilience (auto-respawn, backoff, health checks)
- [x] Add option to define mcpServers in gptsh config per agents either by passing structure or including mcp_servers.json file
- [ ] Refactor MCP tools, introduce decorator registry and refactor existing builtin modules

## User Experience
- [ ] Detect TTY vs non-TTY and provide appropriate UI modes (spinner vs minimal output)
- [ ] Add session history, introduce new `-s [session]` and `--list-sessions` parameters

## Configuration & Agents
- [ ] Implement log redaction for secrets and sensitive data
- [ ] Support logging into file

## Workflows
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
