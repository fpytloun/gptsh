# TODO

## MCP Integration
- [ ] Improve MCP lifecycle resilience (auto-respawn, backoff, health checks)
- [ ] Refactor MCP tools, introduce decorator registry and refactor existing builtin modules
- [ ] Use roots to sandbox tools (https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem#method-2-mcp-roots-recommended, https://modelcontextprotocol.io/docs/learn/client-concepts#roots)

#### Tool approval
- [ ] Option to enter reason when denying tool execution (eg. instruct LLM to use tool differently)
- [ ] Always allow tool execution (will whitelist further calls of exact tool+args, only for given session)

## User Experience
- [x] Detect TTY vs non-TTY and provide appropriate UI modes (spinner vs minimal output)
- [ ] Add session history, introduce new `-s [session]` and `--list-sessions` parameters

## Configuration & Agents
- [ ] Implement log redaction for secrets and sensitive data
- [ ] Support logging into file

## Workflows
- [ ] Add support for Workflows.

Workflow is using multiple steps that consists of agents and other execution blocks

### Workflow definition

Location and includes:
- Workflows are defined under the top-level `workflows` key in the merged configuration (global, snippets, project). Standard precedence applies (project overrides global).
- You can and should organize workflows using config includes, e.g., `!include workflows/*.yml` (same mechanism as for agents). This pattern is recommended.

Example config defining `create-git-commit` workflow that is using already defined `committer` agent and shell execution steps:

```yaml
workflows:
  create-git-commit:
    description: "Create git commit from uncommitted changes"
    parameters:
      amend:
        # Will pass amend into workflow steps (shell variable of value 0/1 and into LLM step as user message)
        type: bool      # required [string|bool|int|float|enum]
        default: false  # optional [.type], default null
        required: false # optional [true|false], default false
        help: "Amend changes into last commit"
      push:
        type: bool
        default: false
        required: false
    steps:
      - description: "Obtain diff of changes"
        id: git-changes  # optional, if set can be referenced instead of using index
        on_failure: exit # Can be "exit" (default, will fail and pass output and exitcode), "continue", "ask", "retry"
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
        id: generate-message
        llm:
          agent: committer
          prompt:
            # Uses Liquid templating
            user: |
              Generate single commit message based on provided diff:
              {{ step['git-changes'].stdout }}
      - description: "Commit and push"
        shell:
          env:
            # define msg passed as env variable, reference last step's stdout.
            msg: "{{ step['generate-message'].stdout }}"
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

#### Parameters

- Supported types: string, bool, int, float, enum.
- For enum, define `choices: [a, b, c]`; values are validated against choices.
- `required`: if true and no value is provided (and no `default`), the CLI must error.
- If `default` is set, `required` is effectively false.

Schema/validation errors (invalid/missing parameters or unknown fields) result in exit code `2`.

#### Available steps:

- **llm** - agent execution
- **shell** - shell execution
- **tool** - MCP tool call
- **python** - in-line python code (`python.code`) or calling module (`python.module`)

#### LLM step

- Inherits the agent selection from the CLI/configured default. `llm.agent` is optional and overrides the inherited agent when provided.
- Templating is available in `prompt.*` fields via Liquid.

#### Tool (MCP) step

- Required fields:
  - `server`: name of the MCP server to use.
  - `name`: tool name on that server.
  - `args`: mapping of arguments (Liquid-templated strings) passed to the tool.
- Validation: prior to execution, the workflow runner validates that the MCP server is available, the tool exists on that server, and the provided `args` match the tool's schema. Validation failures surface before starting the workflow. Missing server/tool or connection failures map to exit code `3`; schema/args issues map to exit code `2`.

#### Shell execution

- `shell.shell`: bash|sh|zsh (default: bash on Linux).
- `shell.cwd`: optional working directory for the step (defaults to current process cwd).
- `shell.env`: merged into the inherited environment; templated string values.
- `shell.stdin`: optional templated string passed to the process stdin.
- `shell.command`: interpolation-only Liquid; control tags are disallowed.

#### Step result

- stdout
- stderr
- exit_code
- started_at
- ended_at
- duration_ms
- artifacts (list of paths, e.g., created/updated files)

#### Templating

- Engine: Liquid (python-liquid) with strict variables/filters, no filesystem loaders.
- Interpolation-only by default: disallow control tags (`{% ... %}`) and comment blocks (`{% comment %}...{% endcomment %}`) in most fields for determinism and safety.
- What gets templated (strings only):
  - `llm.prompt.*`, `shell.env.*`, `tool.args.*`, approval messages.
  - `shell.command` is interpolation-only; avoid control tags. Use `{% raw %}...{% endraw %}` to keep literal braces if needed.
- Expressions for decision fields:
  - `when`, `timeout_seconds`, and similar can be provided as Liquid expressions without `{{ }}` and are evaluated to a Python value (e.g., `when: "params.amend and step['git-changes'].exit_code == 0"`).
- Context available:
  - `params`, `step` (results keyed by step `id`), `env` (read-only), `workspace`, `now`, and (in LLM steps) `agent`.
- Examples:
  - Reference previous step stdout: `{{ step['git-changes'].stdout }}`
  - Defaulting and cleanup: `{{ step['generate-message'].stdout | default: '' | strip }}`
  - Convert bool to 0/1 for shell: `{{ params.amend | default: false | downcase | replace: 'true', '1' | replace: 'false', '0' }}`

#### Parameters and injection semantics

- Exposure to steps:
  - `shell`: all parameters are available for templating via `params.*`. In addition, all parameters are exported to the subprocess environment as shell variables named after the parameter (e.g., `$amend`, `$push`). Boolean params are exported as `0`/`1` for convenience. The subprocess environment inherits the current process env with `shell.env` overlaid.
  - `llm`, `tool`, `python`: parameters available in the Liquid context as `params.*`.
- Step addressing:
  - Each step must use a unique `id` if defined. Results are available in `step['id']` in templates.
  - Index-based access MAY be supported as `step[0]`, `step[-1]` depending on implementation; prefer ids for clarity.

#### Error handling (`on_failure`) and control flow

Available `on_failure` options:

- `exit` (default)
- `continue`
- `ask`
- `retry` (additional config: `retry: { max: 2, backoff: { base_ms: 500, factor: 2.0, jitter_ms: 250 } }`)

Notes:
- Retries are attempted only when `on_failure: retry` is set. Backoff is applied per attempt and a step-level `timeout_seconds` (if set) limits each attempt.

Additional options:
- `timeout_seconds` - timeout of given step in seconds, reaching timeout will trigger `on_failure` action

Conditions:
- `when` - condition to skip step, e.g., `when: "params.amend == true"`

### CLI

Usage will be very simple:
```sh
gptsh -w create-git-commit
```

Or with parameters
```sh
gptsh -w create-git-commit --amend --push
```

CLI parameters will be generated based on parameter type:
- `--flag|--no-flag` for bool
- `--name VALUE`
- `--name choice`

There will be also `--list-workflows` parameter that will list available workflows and their description and available parameters.

#### CLI listing format

`--list-workflows` prints for each workflow:
- name, description
- parameters: name, type, required, default, choices (for enum)

#### Exit codes

- On failure, the workflow exits with the first failing step’s `exit_code` if non-zero.
- If not meaningful, map to project codes: `1` generic, `124` timeout, `4` approval denied, `130` interrupt.
