# opencode Agent Instructions

## Goal
Deliver clean, maintainable, and testable code following Clean Code principles (Robert C. Martin).
Applies to all projects — primarily **Python** and **Bash**.

---

## Naming Conventions

| Context | Python | Bash |
|---|---|---|
| Variables | `snake_case` | `snake_case` |
| Functions | `snake_case` | `snake_case` |
| Classes | `PascalCase` | N/A |
| Constants | `UPPER_SNAKE_CASE` | `UPPER_SNAKE_CASE` |
| Files/modules | `snake_case.py` | `kebab-case.sh` or `snake_case.sh` |

---

## SOLID Principles
1. **SRP** – Each module/function has one reason to change.
2. **OCP** – Open for extension, closed for modification.
3. **LSP** – Subtypes must be substitutable for their base types.
4. **ISP** – Don't force clients to depend on methods they don't use.
5. **DIP** – Depend on abstractions, not concretions.

---

## Code Quality Rules

- **Small functions** – Do one thing; if it needs a comment to explain what it does, extract it (→ see *Bloaters*).
- **DRY** – Extract repeated logic; never copy-paste blocks (→ see *Duplicated code*).
- **Fail fast** – Validate inputs at boundaries; raise explicit errors early.
- **Explicit error handling** – Never swallow exceptions; use typed exceptions in Python.
- **No magic numbers/strings** – Name all constants.
- **Structured logging** – Use `logging` (Python) or `logger` functions (Bash); never raw `print`/`echo`.
- **Config externalization** – `.env`, `pyproject.toml`, or config files; never hardcode secrets or paths.
- **Immutability by default** – Prefer `tuple`/`frozenset` in Python; `readonly` in Bash where practical.

---

## Code Smells to Avoid

- **Bloaters** – Functions >20 lines, classes with multiple responsibilities, >3 parameters.
- **Duplicated code** – Apply extract-function/method immediately.
- **God object** – One class doing everything; split by responsibility.
- **Feature envy** – Method uses another class's data more than its own; move it.
- **Primitive obsession** – Wrap domain primitives in named types or dataclasses.
- **Deep nesting** – Invert conditions and return early instead.
- **Message chains** – `a.b().c().d()` — use a facade or intermediate variable.
- **Inconsistent naming** – Ambiguous or misleading identifiers.

---

## Agentic Workflow

Every non-trivial task follows this sequence. Do not skip steps.

1. **Plan** – Use `todowrite` to break the task into discrete steps before touching any file. For heavy multi-phase work, activate `/deepwork`.
2. **Read first** – Read all relevant files before proposing changes. Never modify code you haven't read. Delegate broad discovery to `@explorer`.
3. **Execute** – Edit with `edit` (prefer over `write`). One responsibility per change. Delegate bounded implementation to `@fixer`; UI/UX to `@designer`.
4. **Test** – Run existing tests; add new ones for new behaviour. Tests must pass before proceeding.
5. **Verify** – For 3+ file edits or any backend/API change, delegate review to `@oracle` via `task`. Skip for single-file, docs-only, or config-only changes. Only proceed on PASS.
6. **Simplify** – Run `/simplify` on changed code to catch smells and redundancy.
7. **Commit** – Small atomic commits using Conventional Commits format (see below).
8. **Document** – Update docstrings, README, and config comments affected by the change.

**Failure recovery**: If tests fail, fix the root cause — not the symptom. If `@oracle` flags issues, address them and re-test; never bypass review to ship broken code. If a request is ambiguous, ask the user before acting.

---

## Session Lifecycle

- **Start** — Read MCP Memory for user/project context. Check `git status` for prior uncommitted work.
- **During** — Store key decisions and preferences to MCP Memory as they emerge. Report progress briefly while background jobs run; pause if a result invalidates earlier assumptions.
- **End** — Update MCP Memory with session outcomes. Verify tests pass, lint clean, no secrets staged.

---

## Communication

- Be concise; no sycophancy, no filler phrases, no unsolicited praise.
- No emojis unless the user explicitly requests them.
- Use GitHub-flavoured Markdown for structure; prefer tables and bullets over prose.

---

## Commit Message Format (Conventional Commits)

```
<type>(<scope>): <short summary>

[optional body: what changed and why]
[optional footer: breaking changes, issue refs]
```

Types: `feat` · `fix` · `refactor` · `test` · `docs` · `chore` · `ci`

Examples:
```
feat(auth): add token refresh on 401 response
fix(parser): handle empty input without raising KeyError
refactor(utils): extract validate_path into separate module
```

---

## File Structure

### Python Projects
```
<project>/
├── src/
│   ├── core/          # Business logic — no I/O, no side effects
│   ├── services/      # External interactions (APIs, DB, filesystem)
│   ├── models/        # Dataclasses, Pydantic models, DTOs
│   ├── utils/         # Pure utility functions
│   ├── handlers/      # Entry points (CLI args, HTTP handlers)
│   └── config/        # Config loaders and env parsing
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── .github/workflows/ # CI pipelines
├── pyproject.toml     # Build system, deps, tool config (ruff, mypy, pytest)
├── .env.example       # Document required env vars; never commit .env
└── README.md
```

### Bash Projects
```
<project>/
├── bin/               # Executable entry-point scripts
├── lib/               # Sourced library functions (one concern per file)
├── config/            # Config files sourced at runtime
├── tests/             # bats tests (bash automated testing system)
├── .env.example
└── README.md
```

---

## Language Practices

### Python
- **Virtual environments** – Always use `conda` (miniconda); never install globally.
- **Dependency management** – `pyproject.toml` as the single source of truth; pin versions in `requirements.lock` if needed.
- **Type hints** – Add to all public functions; run `mypy --strict` in CI.
- **Linting/formatting** – `ruff check` + `ruff format` (replaces flake8/black/isort).
- **Testing** – `pytest` with `pytest-cov`; aim for >80% coverage on `src/core/` and `src/services/`.
- **Docstrings** – Google style for all public functions, classes, and modules.
- **Dataclasses/Pydantic** – Prefer over raw dicts for structured data.

### Bash
- **Shebang** – Always `#!/usr/bin/env bash`.
- **Strict mode** – Every script starts with `set -euo pipefail`.
- **Shellcheck** – All scripts must pass `shellcheck` with no warnings.
- **Functions over inline code** – Extract any logic >5 lines into a named function in `lib/`.
- **Quoting** – Always double-quote variables: `"$var"`, `"${array[@]}"`.
- **Error messages** – Write to stderr: `echo "error: ..." >&2`.
- **Exit codes** – Use meaningful exit codes; document them in the script header.
- **No global state mutations** – Functions should not modify caller variables without `local`.

---

## Development Practices

- **Git** – Feature-branch workflow; squash-merge PRs; no force-push to `main`.
- **Pre-commit** – `ruff`, `mypy`, `shellcheck`, `pytest` (fast unit tests only).
- **CI** – Full test suite on every PR; block merge on failures; enforce coverage.
- **Security** – No secrets in code or git history; use `detect-secrets` in pre-commit.
- **Observability** – Structured logs with level, timestamp, and context; no bare `print`.

---

## opencode Tool Usage

| Task | Tool / Skill |
|---|---|
| Plan and track multi-step work | `todowrite` |
| Search for files | `glob` (never `find` via bash) |
| Search file contents | `grep` (never `grep` via bash) |
| Read a file | `read` (never `cat` via bash) |
| Edit an existing file | `edit` (prefer over `write`) |
| Create a new file | `write` |
| Run shell commands | `bash` |
| Delegate to a specialist | `task` (see Specialists below) |
| Clean up changed code | `/simplify` skill |
| Heavy multi-phase sessions | `/deepwork` skill |
| Risky or parallel work | `/worktrees` skill |
| Learn from past sessions | `/reflect` skill |
| Map an unfamiliar codebase | `/codemap` skill |
| Inspect dependency internals | `/clonedeps` skill |
| Read user/project memory | `memory_search_nodes`, `memory_open_nodes` |
| Store user/project memory | `memory_create_entities`, `memory_add_observations` |

**Parallel tool calls**: Make all independent tool calls in the same response block to maximise efficiency. Don't background trivially-fast or dependency-linked tasks — sequential is faster than task overhead.

**Verification gate**: Any task touching 3+ files or changing an API/backend MUST be reviewed by `@oracle` (via `task`) before reporting completion to the user.

---

## Specialists

oh-my-opencode-slim is installed. Delegate via `task` rather than doing all work inline. Reuse prior sessions by passing the previous `task_id`; prefer the most recently used matching session to preserve context and reduce cost.

| Agent | When to delegate |
|---|---|
| `@explorer` | Codebase discovery, file/symbol search, pattern matching — delegate before planning (2× faster, half the cost) |
| `@librarian` | External docs, library APIs, web research, version-specific behaviour — never guess at library behaviour |
| `@oracle` | Architecture decisions, complex debugging, code review, simplification strategy — do not skip for significant changes |
| `@designer` | UI/UX work, responsive layouts, visual polish, component design — preserve designer output exactly in follow-up phases |
| `@fixer` | Bounded mechanical implementation across multiple files — split large changes into parallel bounded lanes |

Never duplicate work already delegated to a running background agent.

---

## Operational Best Practices

### Background Task Discipline & Job Board

Use `background: true` for independent specialist work. Don't poll — wait for hook-driven completion. If no independent work remains while jobs run, stop and let the completion event resume the workflow. Track each task's agent, purpose, and file ownership to avoid conflicts.

#### Background Job Board
SENTINEL: background-job-board-v2
Do not poll running jobs. Wait for hook-driven completion, or use `cancel_task` only for explicit cancellation. Reconcile terminal jobs before final response.
Completed or reconciled sessions are reusable by alias for the same specialist/context.
Timed-out running sessions are recoverable by alias for safe resume after a live busy signal.
Cancelled or errored sessions are not reusable.

##### Active / Unreconciled
- none

##### Reusable Sessions
- alias / session_id / agent / status
  Objective: one-line summary of what was done
  Context read by alias: files read with line counts

### LSP-Based Validation

After editing code, use the language server to catch errors before running tests:

```python
# After editing a Python file, check for type errors and diagnostics
lsp_diagnostics -> fix any found -> lsp_format_document if needed
```

- Run `lsp_diagnostics` on edited files to catch type errors, missing imports, and warnings.
- Use `lsp_format_document` to keep code formatted consistently.
- For project-wide checks, use `lsp_workspace_diagnostics` after opening the relevant files.

### Shell-Based Test Execution

Run tests via the `bash` tool, not inside editors or inlined:

```bash
# Python
pytest tests/ -x --tb=short

# Linting
ruff check src/ && ruff format --check src/
mypy src/

# Bash
shellcheck bin/*.sh lib/*.sh
bats tests/
```

- Run the full test suite (or the relevant test subset) after every implementation phase.
- Let the exit code and output determine pass/fail — do not skip actual test execution.

### MCP Memory (Persistent Knowledge)

MCP Memory stores persistent knowledge across sessions so the agent doesn't start blank each time.

**Session start — read memory:**
- `memory_search_nodes` to find user preferences ("preferred tools", "naming conventions", "ssh over pat")
- `memory_open_nodes` on matched entities to retrieve full observations

**During work — store context:**
- `memory_add_observations` to append decisions, architecture choices, and project conventions to existing entities
- `memory_create_entities` for new topics (e.g. a new project's tech stack)
- `memory_create_relations` to link related knowledge

**What to track:**
| Entity | Observations to store |
|---|---|
| `user-preferences` | Preferred tools, auth methods (SSH > PAT), config patterns, naming style deviations |
| `project-conventions` | Tech stack, architecture decisions, testing setup, CI choices |
| `session-decisions` | Root cause fixes, workarounds, library gotchas, why something was done a particular way |
| `reusable-patterns` | Scripts, workflows, snippets developed that are likely reusable |

**Enforcement (critical):** Before delivering your final response in any session where a
significant decision, architecture choice, or user preference was discussed, call
`memory_add_observations` to persist it. Do not rely on later sessions to rediscover it.
A decision that isn't stored in memory effectively never happened for the next session.

### Config Changes Require Restart

opencode loads all configuration at startup and does not hot-reload: changes to `opencode.json`, `oh-my-opencode-slim.json`, agent prompt files, skills, or MCP configs take effect **on the next opencode run**. After editing any config file, restart opencode for the changes to apply.

### Error Recovery (Config Escape Hatches)

If a config change breaks opencode's startup, use these environment variables to recover:

```bash
# Skip the project's local opencode.json and start from globals only
OPENCODE_DISABLE_PROJECT_CONFIG=1 opencode

# Load an explicit alternate config
OPENCODE_CONFIG=/path/to/backup.json opencode

# Skip external plugins
OPENCODE_PURE=1 opencode
```

This lets you edit and fix the broken config from inside opencode.
