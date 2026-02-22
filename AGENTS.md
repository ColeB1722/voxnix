# Agent Instructions

## Foundational Context

Before working on any component, read `docs/architecture.md` for the full architectural design, decision log, and constraints. All implementation must align with the decisions documented there.

Key sections to understand before starting:
- **Foundational MVP** — defines the current build scope
- **Trust Model** — informs all security and multi-tenancy decisions
- **Implementation** — tech stack, glue layer, tool architecture, deployment workflow
- **Development Approach** — methodology per component (SDD, TDD, or just build it)

## Project Structure

```
voxnix/
+-- flake.nix              # Top-level flake (flake-parts)
+-- parts/                  # flake-parts modules
+-- nix/
|   +-- host/              # NixOS appliance configuration
|   +-- modules/           # Reusable workload modules (git, fish, tailscale, etc.)
|   +-- images/            # Precompiled image definitions
+-- agent/                  # Python — PydanticAI orchestrator agent
|   +-- tools/             # Agent tool definitions (CLI wrappers, Nix glue)
|   +-- chat/              # Telegram integration layer
|   +-- nix_gen/           # Nix expression generator (JSON spec output)
+-- api/                    # API server (future)
+-- frontends/              # TUI / web dashboard (future)
+-- docs/                   # Architecture docs
+-- justfile                # Task runner
```

## Development Conventions

- **Nix formatting:** nixfmt via treefmt
- **Python formatting:** ruff
- **Task runner:** just (see justfile for available recipes)
- **Dev environment:** `nix develop` provides all tools
- **Commits:** conventional commits preferred (feat:, fix:, docs:, etc.)

## Key Design Decisions

- Agent generates JSON specs, never Nix syntax — Nix functions consume specs and compose modules
- All secrets via agenix, injected as env vars at runtime
- Containers use `privateNetwork=true` always — inter-container comms via shared bridge
- Agent runs on the host (needs host-level access to machinectl, extra-container, ZFS)
- Telegram chat ID is the user identity — agent enforces ownership scoping

## When Building Nix Components

- Write modules, test with `nix build`, iterate
- Modules should be composable and independently testable
- The `mkContainer` Nix function consumes JSON specs — keep the interface clean

## When Building Python Components

- Use spec-kit SDD flow for complex features (spec -> plan -> tasks -> implement)
- TDD for glue layer (CLI wrappers, Nix expression generator, output parsers)
- PydanticAI for agent framework — tools are Pydantic models
- Logfire for all observability

## Code Review Workflow (CodeRabbit)

Run CodeRabbit once per PR, when the PR is ready to merge — not mid-branch after every commit.

```
coderabbit review --type committed --base main --plain
```

### Triage protocol

Every finding gets one of three dispositions — never silently ignore:

| Disposition | Criteria | Action |
|---|---|---|
| **Fix now** | Bug in this PR's own code; correctness risk; trivial to fix | Fix, commit, push |
| **Track** | Pre-existing code; out of scope for this PR; nitpick with real merit | Open a GitHub issue (see format below) |
| **Skip** | Not actually needed (e.g. `@pytest.mark.asyncio` with `asyncio_mode = "auto"`); verified harmless | Note justification in the PR comment |

After triage, post a single PR comment summarising all three columns with issue links for anything tracked.

### Issue format for tracked findings

Each tracked finding becomes a GitHub issue with this structure:

```
## Source
CodeRabbit review of PR #N (branch-name).

## Finding
<finding verbatim or paraphrased>

## Affected files
- path/to/file.py — line range / function name

## Suggested fix
<concrete fix or code snippet>
```

Use existing labels where they fit (`bug`, `enhancement`, `documentation`). Don't create custom labels.

## Issue Backlog

Open issues represent tracked tech debt and deferred findings. Before starting work on any component, run:

```
gh issue list
```

Check for open issues touching the files or subsystem you are about to modify. If an issue is directly addressed by planned work, fix it in the same PR and close it. If it is adjacent but not the focus, note it in the PR description.

Do not re-open or re-create issues that already exist. Do not let findings accumulate in PR comments without a corresponding issue.
