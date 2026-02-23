# Implementation Plan: MVP Completion + Multi-Tenancy

## Context

The foundational MVP is nearly complete. The core pipeline — JSON spec → Nix module composition → `extra-container` — is proven and working end-to-end. The PydanticAI agent creates, destroys, starts, stops, and lists containers via Telegram. Ownership scoping is enforced at the framework level through `VoxnixDeps.owner` (Telegram chat_id). Logfire tracing, agenix secrets, ZFS storage layout, and the deploy workflow are all in place.

**Appliance state:** The appliance is provisioned and running at `192.168.8.146` (Hyper-V Gen 2 VM on Windows 11 Pro, 16GB RAM). The agent service is active and the Telegram bot responds. There is a leftover `dev` container from last session's E2E testing — clean it up before starting new work:

```bash
ssh admin@192.168.8.146 "sudo extra-container destroy dev"
```

### What's done

All verified E2E last session:
- `/start`, `/help`, free-form LLM responses (openrouter:anthropic/claude-haiku-4.5)
- Module discovery (`fish`, `git`, `workspace`)
- Container create, list (running + stopped), stop, start, destroy — all via Telegram bot
- Ownership scoping, per-chat locking, filtered workload listing
- Logfire instrumentation, agenix secrets, deploy workflow (`just deploy 192.168.8.146`)

### What's missing

Two gaps remain before the MVP scope (defined in `docs/architecture.md § Foundational MVP`) is fully satisfied:

1. **Tailscale module** — listed in the MVP scope table but not implemented. Without it, containers are reachable only from the host's local network. Users cannot SSH or connect to services inside containers remotely.
2. **ZFS dataset management** — listed in MVP Build Steps item 4 (`create_zfs_dataset`) but not implemented. The `storage.nix` disko layout defines the `tank/users/<chat_id>/containers/<name>/workspace` hierarchy, and the `workspace` module creates the `/workspace` mount point inside containers, but nothing creates the host-side datasets at runtime or wires up the bind mount.

Both gaps are also prerequisites for multi-tenancy. Tailscale gives each user's containers their own network identity. ZFS datasets give each user isolated, quota-controlled persistent storage. Closing these gaps completes the MVP and simultaneously lays the foundation for multi-user support.

Multi-tenancy itself (as defined in `docs/architecture.md § Trust Model & Multi-Tenancy`) is mostly already built. The ownership model, scoping enforcement, per-chat locking, and filtered workload listing are implemented. What remains is the resource isolation layer: per-user ZFS quotas and per-container Tailscale enrollment.

### Deferred issues from last session

The previous session handoff (`docs/handoff.md`) flagged three issues as immediate priorities for this session. They are small, improve quality of life, and one is a bug that could cause confusing errors during multi-tenancy testing. They become Phase 0 of this plan.

- **#46** — Markdown formatting in agent responses. The LLM generates Markdown (`**bold**`, backticks) but responses are sent as plain text — raw markers appear in Telegram.
- **#52** — Missing `logger.error` in destroy/start/stop failure paths. `create_container` logs to both `logfire.error` and `logger.error`; the other three only use `logfire.error`, so failures are invisible in journalctl when Logfire isn't configured.
- **#12** — No name validation on destroy/start/stop tools. The `ContainerSpec` validator enforces name rules for creation, but the agent can attempt invalid names on the other operations, producing confusing errors.

---

## Relationship to the Architecture

This plan implements components already designed in `docs/architecture.md`. No new architectural decisions are introduced — everything below is filling in specified but unbuilt pieces.

| Architecture section | What it specifies | What this plan builds |
|---|---|---|
| **Foundational MVP § MVP Scope** | Base modules: git, fish, tailscale, workspace | `nix/modules/tailscale.nix` |
| **Foundational MVP § MVP Build Steps** | Core tools include `create_zfs_dataset` | ZFS dataset tools + integration into container lifecycle |
| **Persistence Model § Host Storage — ZFS** | `tank/users/<chat_id>/containers/<name>/workspace` layout | Runtime dataset creation, bind mount wiring, quota enforcement |
| **Trust Model § Multi-tenancy model** | Agent enforces per-user scoping; ZFS quotas per user | Per-user dataset roots with quotas; ownership already enforced |
| **Networking Model § Private access — Tailscale** | Each container runs its own `tailscaled` and gets its own tailnet identity | Tailscale NixOS module, auth key injection |
| **Secrets Management § agenix** | Tailscale auth keys managed via agenix | Reusable auth key in `agent-env`, passed through container spec |
| **Agent Tool Architecture § Core tools** | `create_zfs_dataset` wraps `zfs create` | Python tool + integration into `create_container` flow |

---

## What We Are Building

### Phase 0: Deferred Fixes (Issues #46, #52, #12)

**Why first:** These were explicitly deferred from last session as the intended opening moves. #46 is visible on every single interaction (raw Markdown in Telegram). #52 is a 3-line copy-paste. #12 is a bug that will cause confusing errors when testing with multiple users. Knocking these out first means every subsequent phase is tested against a cleaner baseline.

**Target: single PR, single commit batch.**

#### 0a. Issue #46 — Markdown formatting in agent responses

**Affected files:** `agent/agent.py` (system prompt), `agent/chat/handlers.py` (`reply_text` call).

Two options documented in the handoff:

1. **Quick:** Add to system prompt: `"Do not use Markdown formatting. Respond in plain text only."`
2. **Proper:** Convert LLM Markdown to Telegram's supported subset and pass `parse_mode="MarkdownV2"` or `parse_mode="HTML"` to `reply_text()`.

**Decision: Option 1 (quick) for now.** The proper approach requires a Markdown-to-Telegram converter that handles escaping correctly (MarkdownV2 is notoriously finicky). That's a yak-shave. A system prompt instruction eliminates the problem immediately and can be upgraded later. Add a one-line instruction to the system prompt in `agent/agent.py`.

Note: `/start` and `/help` static messages already had MarkdownV2 removed in PR #45 — this issue is only about LLM-generated responses.

#### 0b. Issue #52 — `logger.error` in destroy/start/stop failure paths

**Affected file:** `agent/tools/containers.py` — three failure branches.

`create_container` already logs failures to both `logfire.error` and `logger.error`. The other three operations (`destroy_container`, `start_container`, `stop_container`) only use `logfire.error`. Add matching `logger.error` calls to each failure branch for journalctl visibility. Direct copy of the pattern from `create_container`.

#### 0c. Issue #12 — Name validation on destroy/start/stop tools

**Affected file:** `agent/agent.py` — `tool_destroy_container`, `tool_start_container`, `tool_stop_container`.

Extract the name validation regex from `ContainerSpec` into a shared utility (or reuse it directly) and validate the `name` argument at the top of each tool function. Return a clear error message for invalid names rather than passing them through to CLI commands that produce cryptic errors.

Alternatively, create a lightweight `validate_container_name(name: str) -> str | None` function in `agent/nix_gen/models.py` that returns an error string or None, and call it from each tool.

#### 0d. Tests

- Update `agent/tests/test_containers.py` — verify `logger.error` is called in failure paths.
- Add name validation tests for destroy/start/stop tools (or add to `test_models.py` if the validator is shared).
- Update `agent/tests/test_chat_handlers.py` if the system prompt change affects test expectations.

---

### Phase 1: ZFS Dataset Management

**Why first (after Phase 0):** The container creation flow needs to create persistent storage before the container starts, so the bind mount target exists. This is a dependency for both the workspace persistence story and multi-tenancy quotas.

#### 1a. ZFS dataset tools (`agent/tools/zfs.py`)

New Python module with three async functions, all wrapped in `logfire.span()` and using `run_command()` from `agent/tools/cli.py`:

- **`create_user_datasets(owner: str) → ZfsResult`**
  Ensures the per-user dataset root exists: `zfs create -p tank/users/<owner>`. Idempotent — succeeds if the dataset already exists. Called automatically on first container creation for a new user.

- **`create_container_dataset(owner: str, container_name: str) → ZfsResult`**
  Creates `tank/users/<owner>/containers/<container_name>/workspace`. Calls `create_user_datasets` first to ensure the parent chain exists. Returns the host-side mount path (`/tank/users/<owner>/containers/<container_name>/workspace`).

- **`destroy_container_dataset(owner: str, container_name: str) → ZfsResult`**
  Wraps `zfs destroy -r tank/users/<owner>/containers/<container_name>`. Called by `destroy_container` after the container is torn down. Recursive (`-r`) to catch the workspace and any future child datasets (cache, etc.).

`ZfsResult` is a simple dataclass mirroring `ContainerResult`: `success`, `name`, `message`, `error`, `mount_path`.

#### 1b. Wire ZFS into the container creation flow

Modify `agent/tools/containers.py :: create_container()`:
1. Before generating the Nix expression, call `create_container_dataset(spec.owner, spec.name)`.
2. If dataset creation fails, return early with an error — don't proceed to container creation.
3. Pass the resulting `mount_path` into the Nix expression generator so `mkContainer` can configure the bind mount.

Modify `agent/tools/containers.py :: destroy_container()`:
1. After successful container destruction, call `destroy_container_dataset(owner, name)`.
2. The owner is needed here — add it as a parameter (the agent tool already has it from `ctx.deps.owner`).

**Error path cleanup:** If `create_container_dataset` succeeds but `extra-container create` fails, the orphaned dataset should be cleaned up. The implementation should call `destroy_container_dataset` in the error path of `create_container()`.

#### 1c. Extend the container spec and Nix expression generator

Add an optional `workspace_path` field to `ContainerSpec` in `agent/nix_gen/models.py`:

```
workspace_path: str | None = None
```

When set, `generate_container_expr()` includes it in the JSON spec passed to `mkContainer.nix`.

#### 1d. Wire bind mounts into `mkContainer.nix`

When `spec.workspace` is present (a host path string), add a `bindMounts` entry:

```
bindMounts."/workspace" = {
  hostPath = spec.workspace;
  isReadWrite = true;
};
```

This connects the ZFS dataset on the host to the `/workspace` directory inside the container (created by the `workspace` module's `systemd.tmpfiles.rules`).

When `spec.workspace` is absent (container created without workspace persistence), no bind mount is added — the workspace module still creates `/workspace` as an ephemeral directory inside the container.

#### 1e. Update `ReadWritePaths` in `agent-service.nix`

The agent's systemd service uses `ProtectSystem=strict` with explicit `ReadWritePaths`. The ZFS tools run `zfs create` and `zfs destroy` which need write access to ZFS management paths. Verify that the service can invoke `zfs` commands — if not, add the necessary paths. This is the exact class of problem documented in `AGENTS.md § Deployment Debugging Strategy` ("Works as admin, fails in service → Check `systemctl show` environment; test in the service namespace").

#### 1f. Tests

- `agent/tests/test_zfs.py` — unit tests for all three ZFS functions with mocked `run_command`.
- Update `agent/tests/test_containers.py` — test that `create_container` calls dataset creation before Nix expression generation, and `destroy_container` calls dataset cleanup after destruction. Test the error path cleanup (dataset created but container creation fails → dataset destroyed).
- Update `agent/tests/test_generator.py` — test that `workspace_path` is included in the generated Nix expression when present, and omitted when absent.
- Update `agent/tests/test_models.py` — validate the optional `workspace_path` field.

---

### Phase 2: Tailscale Module

**Why second:** With ZFS wired up, containers already have persistent workspaces. Tailscale adds remote access — the user can SSH into their containers from anywhere on their tailnet.

#### 2a. Tailscale auth key in agenix

Add `TAILSCALE_AUTH_KEY` to the `agent-env.age` secrets file. This is a **reusable, ephemeral** Tailscale auth key generated from the admin's Tailscale admin console. Reusable so multiple containers can use it; ephemeral so devices auto-expire if the container is destroyed and never re-registered.

The architecture doc's MVP scope table explicitly puts "Dynamic Tailscale API key generation" out of scope. A single reusable auth key managed by the admin is the MVP approach.

Add the env var to `VoxnixSettings` in `agent/config.py`:

```
tailscale_auth_key: SecretStr | None = None
```

Optional — the agent can function without Tailscale (containers are still reachable from the host LAN). But if a user requests the `tailscale` module and no auth key is configured, the agent should return a clear error rather than creating a broken container.

#### 2b. Tailscale NixOS module (`nix/modules/tailscale.nix`)

```nix
{ pkgs, ... }:
{
  services.tailscale.enable = true;

  # Tailscale needs access to /dev/net/tun for kernel-mode networking.
  # systemd-nspawn containers typically have this available.
  # Fallback: services.tailscale.useRoutingFeatures = "client" for userspace mode.

  networking.firewall.allowedUDPPorts = [ 41641 ];  # Tailscale WireGuard

  # The auth key is injected as an environment variable (TAILSCALE_AUTH_KEY)
  # and consumed by a oneshot service that runs `tailscale up` on first boot.
  systemd.services.tailscale-autoconnect = {
    description = "Automatic Tailscale enrollment";
    after = [ "tailscaled.service" ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig.Type = "oneshot";
    script = ''
      sleep 2
      ${pkgs.tailscale}/bin/tailscale up \
        --auth-key="$TAILSCALE_AUTH_KEY" \
        --hostname="$VOXNIX_CONTAINER" \
        --accept-routes=false \
        --ssh
    '';
  };
}
```

The `VOXNIX_CONTAINER` env var is already set by `mkContainer.nix` (from `spec.name`). The `TAILSCALE_AUTH_KEY` env var needs to be injected into the container — see 2c.

The `--ssh` flag enables Tailscale SSH, so users can `ssh root@<container-name>` via their tailnet without managing SSH keys inside the container.

#### 2c. Auth key injection into containers

The Tailscale auth key lives on the host (in `agent-env`, decrypted to `/run/agenix/agent-env`). It needs to reach the container's environment. Two approaches:

**Approach A — environment variable in mkContainer:** Add `TAILSCALE_AUTH_KEY` to `environment.variables` in the container config when the `tailscale` module is selected. This requires the spec to carry the auth key value, which means the agent reads it from settings and includes it in the spec.

**Approach B — bind-mount the secret file:** Bind-mount a file containing just the auth key into the container, and have the tailscale-autoconnect service read from the file.

**Decision: Approach A.** It's simpler, aligns with how `VOXNIX_OWNER` is already injected, and the "secret" is a reusable auth key with limited scope (it can only add devices to your own tailnet). The container's `/etc/set-environment` is in the Nix store on the host, readable by root — same trust boundary as the current `VOXNIX_OWNER` injection.

Implementation:
- Add an optional `tailscale_auth_key` field to `ContainerSpec`.
- When present, `mkContainer.nix` adds `environment.variables.TAILSCALE_AUTH_KEY = spec.tailscaleAuthKey;` to the container config.
- The agent populates this field from `VoxnixSettings.tailscale_auth_key` when the spec includes the `tailscale` module.
- If the `tailscale` module is requested but no auth key is configured, the agent returns an error before attempting creation.

#### 2d. Update agent tool and system prompt

Modify `tool_create_container` in `agent/agent.py`:
- After constructing `ContainerSpec`, if `"tailscale"` is in `spec.modules`, read the auth key from settings and set `spec.tailscale_auth_key`.
- If the key is not configured, return a clear error message.

Update the system prompt to mention that containers with the `tailscale` module get a hostname on the tailnet matching the container name, and users can SSH in via Tailscale SSH.

#### 2e. Verify `/dev/net/tun` availability in containers

systemd-nspawn typically provides `/dev/net/tun` to containers, but this needs verification on the actual appliance. Test during deployment:

```bash
ssh admin@192.168.8.146 "sudo nixos-container run <test-container> -- ls -la /dev/net/tun"
```

If unavailable, Tailscale's userspace networking mode is the fallback. If this is the case, update the module to set `services.tailscale.useRoutingFeatures = "client"`.

#### 2f. Tests

- `agent/tests/test_tailscale.py` — test auth key injection logic, error when key missing but tailscale module requested.
- Update `agent/tests/test_generator.py` — test that `tailscale_auth_key` appears in generated Nix when present.
- Update `agent/tests/test_models.py` — validate the optional `tailscale_auth_key` field.

---

### Phase 3: Multi-Tenancy — Per-User ZFS Quotas

**Why third:** With Phases 1 and 2 complete, multiple users can each create containers with persistent storage and Tailscale access. The missing piece is resource limits — preventing one user from consuming all disk space.

#### 3a. Quota configuration

Add a `ZFS_USER_QUOTA` setting to `VoxnixSettings` (default: `"10G"`). This is the quota applied to each user's root dataset (`tank/users/<chat_id>`).

```
zfs_user_quota: str = "10G"
```

#### 3b. Apply quotas in `create_user_datasets()`

After creating `tank/users/<owner>`, run:
```
zfs set quota=<quota> tank/users/<owner>
```

This limits the total space used by all of a user's container workspaces combined. Individual containers share the user's quota — no per-container quota needed for the MVP (the user can allocate space across containers as they see fit).

Idempotent — setting a quota on an existing dataset just updates the limit.

#### 3c. Quota query tool (optional, low effort)

Add `get_user_quota(owner: str) → ZfsQuotaInfo` that wraps `zfs get -Hp quota,used,available tank/users/<owner>`. The agent can report storage usage when asked.

Register as an agent tool so users can ask "how much storage am I using?"

#### 3d. Tests

- Update `agent/tests/test_zfs.py` — test quota application during user dataset creation.
- Test for the quota query tool.

---

## Implementation Order and Approach

Following `docs/architecture.md § Development Approach`:

| Phase | Component | Approach | Estimated Scope |
|---|---|---|---|
| **0a** | #46 — Markdown formatting | Just build it — one-line system prompt addition | ~1 line in `agent.py` |
| **0b** | #52 — `logger.error` gaps | Just build it — copy existing pattern | ~9 lines in `containers.py` |
| **0c** | #12 — Name validation | TDD — shared validator, call from each tool | ~30 lines across `models.py` + `agent.py` |
| **0d** | Phase 0 tests | — | Updated test files |
| **1a** | `agent/tools/zfs.py` | TDD — CLI wrapper, clear input/output contract | New file, ~120 lines |
| **1b** | Container lifecycle integration | TDD — mock ZFS calls, verify ordering | Modify `containers.py`, ~30 lines |
| **1c** | Spec + generator extension | TDD — workspace_path field and Nix output | Modify `models.py` + `generator.py`, ~20 lines |
| **1d** | `mkContainer.nix` bind mounts | Build + test with `nix eval` | Modify `mkContainer.nix`, ~15 lines |
| **1e** | `ReadWritePaths` check | Deployment debugging — test in service namespace | Modify `agent-service.nix` if needed |
| **1f** | Tests for Phase 1 | — | New + updated test files |
| **2a** | Auth key in agenix + config | Just build it — env var plumbing | Modify `config.py` + secrets, ~10 lines |
| **2b** | `nix/modules/tailscale.nix` | Build + test with `nix eval` | New file, ~30 lines |
| **2c** | Auth key injection in spec/Nix | TDD — spec field, generator output, mkContainer | Modify 3 files, ~25 lines |
| **2d** | Agent tool + prompt updates | Just build it | Modify `agent.py`, ~15 lines |
| **2e** | Verify `/dev/net/tun` | Deployment test on appliance | No code change if it works |
| **2f** | Tests for Phase 2 | — | New + updated test files |
| **3a–d** | ZFS quotas | TDD | Modify `zfs.py` + `config.py`, ~40 lines |

Total new/modified production code: ~350 lines across ~10 files, plus tests.

---

## What This Completes

After all four phases:

- **MVP scope is 100% satisfied** — all four base modules (git, fish, tailscale, workspace), all core tools including `create_zfs_dataset`, persistent workspaces, and remote access.
- **Multi-tenancy is functional** — multiple Telegram users can create containers with isolated persistent storage, per-user disk quotas, independent Tailscale identities, and ownership-scoped visibility. The trust model from the architecture doc ("good fences make good neighbors") is fully implemented.
- **The architecture doc's multi-tenancy design requires no further implementation** — identity (chat_id), scoping (VoxnixDeps.owner), ownership enforcement (_check_ownership), and resource isolation (ZFS quotas) are all in place.
- **Deferred quality issues are resolved** — Markdown formatting, logging gaps, and name validation bugs are fixed before they accumulate further.

### What remains after this plan (future iterations, not in scope)

Per the MVP scope table's "Out" column and the architecture doc's future sections:

- `microvm.nix` VMs for hard isolation
- Precompiled image cache
- Dynamic Tailscale API key generation (replacing the static reusable auth key)
- `code-server`, `env-vars`, and additional modules
- Pangolin public access tunneling
- API server and frontends (TUI/web)
- Conversation history persistence (tracked as issue #48)
- CI pipeline and Cachix binary cache

---

## Open Questions

1. **Tailscale auth key rotation:** The reusable auth key has a maximum lifetime set in the Tailscale admin console (default 90 days). When it expires, new containers can't enroll. For now this is an admin responsibility (regenerate and update agenix). Worth tracking as an issue for the "Dynamic Tailscale API key generation" future iteration.

2. **`/dev/net/tun` in containers:** systemd-nspawn typically provides `/dev/net/tun` to containers, but this needs verification on the actual appliance. If unavailable, Tailscale's userspace networking mode (`services.tailscale.useRoutingFeatures = "client"`) is the fallback. Test during Phase 2 deployment.

3. **Container IP reporting:** Currently `list_workloads` reports container IPs from machinectl. With Tailscale, the more useful address is the Tailscale IP/hostname. Consider adding a `tailscale status --json` query to enrich workload listing — but this can be a follow-up enhancement, not a blocker.

4. **ZFS dataset cleanup on failed container creation:** If `create_container_dataset` succeeds but `extra-container create` fails, the orphaned dataset should be cleaned up. The implementation handles this in the error path of `create_container()` (Phase 1b).

5. **`ReadWritePaths` for ZFS commands:** The agent service runs under `ProtectSystem=strict`. ZFS management commands may need paths added to `ReadWritePaths` in `agent-service.nix`. This is the "works as admin, fails in service" pattern from `AGENTS.md` — test in the service namespace during Phase 1e.

---

## Branch and PR Strategy

Per `AGENTS.md § Code Review Workflow`:

- **Phase 0:** Single branch `fix/deferred-cleanup`, single PR. These are small fixes — merge before starting Phase 1.
- **Phases 1–3:** Single branch `feat/multi-tenancy`. Accumulate all work, deploy from the branch tip to test on the appliance (`just deploy 192.168.8.146` works from any branch). Do not open the PR until all phases are implemented and verified working. Run CodeRabbit once when the PR is ready to merge.

```bash
# Phase 0
git checkout -b fix/deferred-cleanup
# implement, test, deploy, verify
# open PR, CodeRabbit, merge

# Phases 1-3
git checkout -b feat/multi-tenancy
# implement phase by phase, deploy + test between phases
# open PR only when stable
~/.local/bin/coderabbit review --type committed --base main --plain
# triage, merge
```
