# gptsh Constitution Specification

**Created**: 2025-01-11  
**Status**: Active  
**Version**: 1.0.0

---

## Executive Summary

This specification formalizes the gptsh project's constitutional principles governing code quality, testing standards, user experience consistency, and performance requirements. These principles are **non-negotiable** and supersede other documentation when conflicts arise.

---

## Functional Requirements

### Code Quality Requirements (CQ)

#### CQ-001: Type Safety
- **Requirement**: All Python code MUST have complete type hints
- **Scope**: Function parameters, return values, class attributes
- **Enforcement**: Ruff linter enforces; PRs checked for violations
- **Rationale**: Type safety enables early error detection, improves IDE support, and documents intended behavior

#### CQ-002: Linting Standards
- **Requirement**: All code MUST pass Ruff linting with zero violations
- **Configuration**: Line-length=100, isort enabled, Black-compatible spacing
- **Enforcement**: Pre-commit hook; CI/CD gate blocks merge on failure
- **Rationale**: Consistent style improves readability and reduces cognitive load

#### CQ-003: Import Organization
- **Requirement**: Imports MUST follow: stdlib → third-party → first-party
- **Enforcement**: Ruff isort rule (I001) enforced
- **Rationale**: Predictable import order aids code navigation

#### CQ-004: Async-First Implementation
- **Requirement**: All blocking I/O (HTTP, subprocess, file ops) MUST be async
- **Exception**: `asyncio.to_thread` permitted for sync-only libraries
- **Enforcement**: Code review; architecture review for new modules
- **Rationale**: Prevents event loop blocking; enables responsive concurrent operations

#### CQ-005: Variable Naming
- **Requirement**: All variables MUST have descriptive names (no single letters except loop indices)
- **Enforcement**: Code review
- **Rationale**: Self-documenting code reduces cognitive load and bugs

---

### Testing Requirements (TS)

#### TS-001: Test-First Development (MANDATORY)
- **Requirement**: Tests MUST be written before implementation
- **Process**:
  1. Write failing test (RED)
  2. Seek user approval for test design
  3. Verify test fails
  4. Implement minimal code to pass (GREEN)
  5. Refactor while keeping tests green
- **Enforcement**: Git history review in PR; test timestamps checked
- **Rationale**: Ensures requirements are clear before coding; documents expected behavior

#### TS-002: Unit Test Coverage
- **Requirement**: New code MUST achieve ≥80% coverage
- **Tools**: pytest with pytest-asyncio for async code
- **Mocking**: External dependencies (LLM APIs, MCP servers) MUST be mocked
- **Enforcement**: Coverage report in CI; warning if < 80%, auto-comment on PR
- **Rationale**: High coverage catches regressions early

#### TS-003: Async Test Patterns
- **Requirement**: Async functions MUST be tested with pytest-asyncio
- **Patterns**:
  - Use `async def test_*` for async tests
  - Mock external API calls with async mocks
  - Use `asyncio.run()` for test fixtures if needed
- **Rationale**: Ensures async behavior is properly tested

#### TS-004: Integration Tests
- **Requirement**: Critical integration points MUST have integration tests
- **Scope**: MCP server interactions, LLM API calls, CLI flag combinations
- **Isolation**: Use mock MCP servers (stdio and SSE/HTTP)
- **Rationale**: Unit tests alone miss integration bugs

#### TS-005: Chaos Testing
- **Requirement**: Resilience MUST be verified via chaos tests
- **Scenarios**: Kill MCP servers, simulate network failures, timeout operations
- **Rationale**: Builds confidence in error recovery mechanisms

#### TS-006: Snapshot Testing
- **Requirement**: CLI output MUST have snapshot tests (TTY vs non-TTY)
- **Tool**: pytest snapshot testing or manual fixture files
- **Rationale**: Catches unintended output changes

#### TS-007: Test Structure
- **Requirement**: Mirror source structure in tests
- **Pattern**: `gptsh/tests/test_<module>.py` for `gptsh/<module>.py`
- **Fixtures**: Centralize in `conftest.py` for state isolation
- **Rationale**: Makes tests easy to locate and maintain

---

### Security Requirements (SEC)

#### SEC-001: Secrets Management
- **Requirement**: API keys/tokens MUST come from environment variables only
- **Config Syntax**: Use `${VAR_NAME}` for secret references
- **Enforcement**: Pre-commit hook scans for hard-coded secrets
- **Rationale**: Prevents accidental credential exposure

#### SEC-002: Logging Security
- **Requirement**: Logs MUST NOT contain secrets, headers, or request bodies
- **Scope**: All logging levels including DEBUG
- **Redaction**: Planned feature for automatic sensitive data removal
- **Enforcement**: Manual code review + automated patterns
- **Rationale**: Prevents credential leakage in log files

#### SEC-003: Approval Gates
- **Requirement**: Destructive operations MUST require explicit approval
- **Destructive**: File writes, process execution, network state changes
- **Non-TTY**: Auto-deny destructive operations by default
- **Timeout**: Approval prompts timeout with default deny
- **Rationale**: Prevents unintended system changes

#### SEC-004: Shell Execution
- **Requirement**: Shell commands MUST pass through approval gate
- **Exception**: Read-only commands may auto-approve if pre-approved in config
- **Rationale**: Prevents unauthorized command execution

#### SEC-005: Least Privilege
- **Requirement**: Subprocess execution MUST use least-privilege principle
- **Practice**: Pass explicit argv, avoid shell=True, restrict working directory
- **Rationale**: Reduces attack surface

---

### User Experience Requirements (UX)

#### UX-001: I/O Consistency
- **stdin**: Read non-interactive input and append to prompt; detect binary content
- **stdout**: LLM output ONLY (clean, pipeable)
- **stderr**: Progress bars, approval prompts, logs

**Rationale**: Enables shell composition and scripting

#### UX-002: Output Formats
- **Default**: Markdown with Rich rendering
- **Text Mode**: Plain output for scripting (`-o text`)
- **Format Selection**: Via `-o/--output` flag
- **Rationale**: Supports different use cases

#### UX-003: Progress Indication
- **Requirement**: Long operations MUST show progress
- **Medium**: stderr progress bar/spinner
- **Control**: Disable with `--no-progress` for scripting
- **Rationale**: User feedback prevents perception of hanging

#### UX-004: Error Messages
- **Quality**: Clear, actionable messages with context
- **Format**: What failed, why, how to fix
- **Stack Traces**: Only in `--debug` mode
- **Rationale**: Reduces support burden and frustration

#### UX-005: Exit Codes
- **0**: Success
- **1**: Generic failure
- **2**: Configuration error (invalid/missing)
- **3**: MCP connection/spawn failure (after retries)
- **4**: Tool approval denied
- **124**: Operation timeout
- **130**: Interrupted (Ctrl-C)

**Rationale**: Enables error detection in shell scripts

#### UX-006: Graceful Degradation
- **Requirement**: Handle broken pipes, timeouts, interruptions gracefully
- **Practice**: Catch signals, clean up resources, exit cleanly
- **Rationale**: Prevents partial state corruption

---

### Performance Requirements (PERF)

#### PERF-001: Startup Time
- **Target**: CLI cold start < 500ms
- **Measurement**: `time gptsh --help`
- **Tolerance**: 20% regression triggers investigation; 50% blocks merge
- **Rationale**: User expectation for CLI tools

#### PERF-002: Config Loading
- **Target**: < 100ms
- **Measurement**: Time from `main()` to agent resolution
- **Includes**: File I/O, YAML parsing, env expansion
- **Rationale**: Config is on critical path

#### PERF-003: Tool Discovery
- **Target**: < 2s per MCP server
- **Measurement**: Time from server spawn to tools listed
- **Includes**: Connection, handshake, tool enumeration
- **Rationale**: Enables rapid tool availability

#### PERF-004: Streaming Latency
- **Target**: First token < 2s (LLM-dependent)
- **Measurement**: Time from request sent to first token received
- **Note**: Varies by provider and model size
- **Rationale**: User perceives responsiveness

#### PERF-005: Resource Reuse
- **Requirement**: HTTP clients MUST be reused across turns per Agent
- **Pattern**: Store provider client on Agent/LLM implementation
- **Benefit**: Avoids connection overhead
- **Rationale**: Preserves connection pooling benefits

#### PERF-006: Token Budgeting
- **Requirement**: Maintain 20% safety margin from provider limits
- **Behavior**: Summarize if input exceeds budget
- **Configuration**: Per-agent `max_tokens` setting
- **Rationale**: Prevents rate limiting and failures

#### PERF-007: Rate Limiting
- **Requirement**: Handle 429/5xx with exponential backoff + jitter
- **Configuration**: Max retries per provider
- **Rationale**: Resilient to transient failures

#### PERF-008: Memory Management
- **Requirement**: Support session history compaction
- **Feature**: `/compact` command in REPL
- **Behavior**: Summarize + clear history, reduce tokens for next turn
- **Rationale**: Enables long-running REPL sessions

#### PERF-009: Content Storage
- **Requirement**: Multimodal content stored as text markers (no base64)
- **Rationale**: Keeps saved sessions readable and compact

---

### Async Architecture Requirements (ASYNC)

#### ASYNC-001: HTTP Operations
- **Requirement**: Use `httpx.AsyncClient` for all HTTP requests
- **Pattern**: Share single client across turns
- **Cleanup**: Close client on graceful shutdown
- **Rationale**: Async prevents event loop blocking

#### ASYNC-002: Subprocess Operations
- **Requirement**: Use `asyncio.create_subprocess_exec` for subprocesses
- **Note**: Avoid `subprocess.run()` which blocks
- **Cleanup**: Terminate processes on graceful shutdown
- **Rationale**: Async prevents blocking

#### ASYNC-003: File I/O
- **Requirement**: Use `asyncio.to_thread` or async file library
- **Pattern**: Read large files asynchronously
- **Rationale**: Prevents blocking on slow disks

#### ASYNC-004: Timeout & Cancellation
- **Requirement**: All long-running operations MUST accept timeout
- **Pattern**: Use `asyncio.wait_for(operation, timeout=...)`
- **Graceful**: Catch `asyncio.TimeoutError` and clean up
- **Rationale**: Prevents indefinite hangs

#### ASYNC-005: Graceful Shutdown
- **Requirement**: All background tasks MUST be cancellable
- **Pattern**: Store task handles; call `task.cancel()` on shutdown
- **Cleanup**: Close all streams, clients, terminate child processes
- **Rationale**: Prevents resource leaks

#### ASYNC-006: Session Lifecycle
- **Requirement**: Runner owns ChatSession lifecycle
- **REPL Mode**: Single session per agent, reused across turns
- **Cleanup**: Runner/REPL closes session on exit
- **Rationale**: Enables efficient resource sharing

---

### MCP Resilience Requirements (MCP)

#### MCP-001: Remote Server Reconnection
- **Requirement**: Auto-reconnect with exponential backoff + jitter
- **Parameters**: Initial backoff, max backoff, jitter factor
- **Retry**: Indefinite unless `max_retries` configured
- **Readiness**: Run `list_tools()` after connect to verify health
- **Rationale**: Handles temporary network blips

#### MCP-002: Local Server Supervision
- **Requirement**: Supervise local/stdio servers; restart on exit
- **Pattern**: Spawn subprocess; monitor; restart with backoff
- **Cleanup**: Terminate on graceful shutdown
- **Rationale**: Keeps local servers available

#### MCP-003: Tool Caching
- **Requirement**: Cache tools per ChatSession lifetime
- **Invalidation**: Cache persists for session; cleared on new session
- **Refresh**: Optional manual refresh via CLI flag
- **Rationale**: Avoids repeated discovery calls

#### MCP-004: SSE Resilience
- **Requirement**: Resume SSE streams when possible; re-list tools on failure
- **Pattern**: Detect disconnect; attempt reconnect; resync tool list
- **Rationale**: Minimizes tool discovery overhead

#### MCP-005: Docker Support
- **Requirement**: Support `command: docker` for MCP servers
- **Health Check**: Use `list_tools()` as readiness probe
- **Cleanup**: Kill container on graceful shutdown
- **Rationale**: Enables containerized MCP servers

#### MCP-006: Discovery Scheduling
- **Requirement**: Periodic tool discovery at `discovery_interval_seconds`
- **Caching**: Results cached per session
- **Graceful**: Handle temporary unavailability without errors
- **Rationale**: Keeps tool list fresh for long-running sessions

---

## Success Metrics

### Code Quality Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| Linting Pass Rate | 100% | Ruff violations count | Block merge if fails |
| Type Coverage | 100% | Functions with hints | Code review check |
| Secret Leakage | 0 instances | Automated scan | Block merge if found |
| Blocking I/O | 0 outside thread | Code review | Block merge if found |

### Testing Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| Code Coverage | ≥80% | Coverage report | Warn if < 80% |
| Test-First Compliance | 100% | Git history analysis | Verify in PR |
| Integration Tests | All critical paths | Test count per module | Code review check |
| Test Pass Rate | 100% | Pytest exit code | Block merge if fails |

### Security Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| Secret Exposure | 0 instances | Log inspection | Block merge if found |
| Approval Compliance | 100% | Code review | Verify in PR |
| Vulnerability Scan | 0 high/critical | pip-audit or similar | Block merge if found |

### User Experience Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| Clean stdout | 100% | Manual testing | Code review check |
| Exit Code Usage | 100% | Static analysis | Code review check |
| Error Clarity | ≥90% users | User feedback | Gathering metric |
| Broken Pipe Errors | 0 | Integration tests | Test must pass |

### Performance Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| CLI Startup | < 500ms | `time gptsh --help` | Investigate if > 600ms |
| Config Load | < 100ms | Instrumentation | Investigate if > 120ms |
| Tool Discovery | < 2s/server | Instrumentation | Investigate if > 2.4s |
| Regression < 20% | N/A | Benchmark comparison | Investigate if found |
| Regression > 50% | N/A | Benchmark comparison | Block merge if found |

### Resilience Metrics

| Metric | Target | Measurement | Gate |
|--------|--------|-------------|------|
| MCP Recovery | ≥95% success | Chaos test results | Must pass tests |
| Graceful Shutdown | < 2s | Instrumentation | Must complete cleanly |

---

## Compliance Verification

### Pre-Commit Checks
```bash
# Linting
UV_CACHE_DIR=.uv-cache uv run ruff check

# Tests
UV_CACHE_DIR=.uv-cache uv run pytest

# Coverage (optional)
UV_CACHE_DIR=.uv-cache uv run pytest --cov=gptsh --cov-report=term
```

### PR Review Checklist
- [ ] Constitution compliance verified
- [ ] Tests written before implementation
- [ ] Type annotations present and correct
- [ ] Async-first verified (no blocking I/O)
- [ ] Security checklist completed
- [ ] Linting passes (Ruff check)
- [ ] Tests pass (pytest)
- [ ] Coverage ≥80% for new code
- [ ] No hard-coded secrets
- [ ] Documentation updated

### Failure Criteria
**PRs are auto-rejected if**:
- Ruff linting fails
- Pytest fails
- Coverage < 80% without justification
- Security vulnerabilities found
- Hard-coded secrets detected
- Performance regression > 50%

---

## Amendment Process

To amend this constitution:

1. **Propose**: Document rationale and changes in a GitHub issue/discussion
2. **Design**: Create detailed amendment spec with implementation plan
3. **Review**: Get consensus from maintainers
4. **Approve**: Maintainer approval required
5. **Document**: Update this document with version bump and rationale
6. **Migrate**: Execute migration plan for affected code
7. **Communicate**: Announce changes to team

---

## Related Documents

- `AGENTS.md`: Development instructions and runtime guidance
- `.kittify/active-mission/constitution/principles.md`: Constitutional principles
- `pyproject.toml`: Tool configuration (Ruff, pytest)
- `.github/PULL_REQUEST_TEMPLATE.md`: PR checklist

---

**Version**: 1.0.0 | **Ratified**: 2025-01-11 | **Last Amended**: 2025-01-11
