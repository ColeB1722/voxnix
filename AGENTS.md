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

### Branch discipline — keep fixes on the branch until stable

The most common way to lose CodeRabbit coverage is the deployment debug loop: make a small fix, open a PR, merge immediately, repeat. After a few cycles, every fix is already on main and there's nothing left to review.

The rule: **do not merge between fixes during a debug or deployment session.** Accumulate all fixes on the same branch, validate by deploying from the branch tip (not from main — `just deploy` works from any branch), then run CodeRabbit once when the session is stable and the PR is ready.

```
# Deploy from a branch — no need to merge first
git checkout fix/deployment-session
just deploy <ip>          # deploys whatever the local flake evaluates to
# ... fix more things, commit, deploy again ...
~/.local/bin/coderabbit review --type committed --base main --plain
# triage, then merge
```

For **feature development**, the existing workflow is correct — one branch, implement, iterate, CodeRabbit when ready.

For **deployment debugging**, keep fixes batched on a single branch (`fix/deployment-session` or similar) until the system is stable and working.

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

**Every idea, finding, and piece of tech debt gets a GitHub issue.** A thought that lives only in a PR comment, a conversation, or someone's head is a thought that will be lost. Issues are cheap; forgotten context is expensive.

This applies to everything — not just bugs:
- Half-baked feature ideas → issue (label: `enhancement`)
- Tech debt identified during review → issue (label: `bug` or `enhancement`)
- Architectural questions surfaced during implementation → issue
- "We should do X someday" → issue

The issue backlog is the project's memory across sessions. When starting a new feature, the backlog tells you what constraints, ideas, and unresolved questions already exist. Without it, every session starts from zero.

### Before starting work

Open issues represent tracked tech debt and deferred findings. Before starting work on any component, run:

```
gh issue list
```

Check for open issues touching the files or subsystem you are about to modify. If an issue is directly addressed by planned work, fix it in the same PR and close it. If it is adjacent but not the focus, note it in the PR description.

Do not re-open or re-create issues that already exist. Do not let findings accumulate in PR comments without a corresponding issue.
