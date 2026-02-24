# Voxnix — Session Handoff

> **This file is the persistent memory of the project across sessions.**
> Update it at the end of every session before stopping work.
> The next agent reads this before doing anything else.

You are working on **voxnix** — an agentic NixOS container orchestrator. A Telegram bot backed by a PydanticAI agent manages NixOS containers on a self-hosted Hyper-V VM appliance. The agent generates JSON specs → Nix functions compose modules → `extra-container` builds and starts containers.

**Always read `docs/architecture.md` and `AGENTS.md` before starting work.**
**Always run `gh issue list` before starting work to check open tracked debt.**

---

## System state

**Appliance:** `192.168.8.146` (Hyper-V Gen 2 VM, 16GB RAM)
**Agent service:** active, Telegram bot responding
**Current branch:** `main` — PR #77 merged, branch deleted
**Last known good container:** `dev` running, Tailscale enrolled at `100.83.13.65` (this IP changes on re-enrollment)

---

## What is working end-to-end

All three E2E acceptance criteria verified:

1. **Container creation** — bot creates a NixOS container with git, fish, tailscale, workspace modules via `extra-container`
2. **Tailscale enrollment** — container appears in Tailscale admin console tagged `tag:shared`, online, with a stable IP
3. **SSH via Tailscale** — `ssh root@<tailscale-ip>` from the dev machine works (Tailscale SSH enabled via `--ssh` flag)
4. **Workspace persistence** — `/workspace` is a ZFS dataset that survives `stop`/`start` cycles

---

## MVP status: complete ✅

All 6 MVP build steps from `docs/architecture.md` are implemented and verified. All open issues are post-MVP enhancements, not blockers. See architecture.md § Foundational MVP for the full scope definition.

---

## What was built (cumulative, all merged to main via PR #56)

### Phase 0 — Deferred fixes (#46, #52, #12) — closed
- **#46:** System prompt instructs LLM to use plain text, no Markdown
- **#52:** `logger.error` added to destroy/start/stop failure paths
- **#12:** `validate_container_name()` shared validator wired into destroy/start/stop tools

### Phase 1 — ZFS dataset management
- `agent/tools/zfs.py` — `create_user_datasets`, `create_container_dataset`, `destroy_container_dataset`, `get_user_storage_info`
- Container creation provisions `tank/users/<chat_id>/containers/<name>/workspace` before building
- Datasets created with explicit `mountpoint=` at each level so they appear as real host directories for nspawn bind mounts
- `ContainerSpec.workspace_path` flows through generator into `mkContainer.nix` `bindMounts`
- Per-user ZFS quota (default 10G) via `VoxnixSettings.zfs_user_quota`
- Agent tool: `tool_storage_usage` — users can ask "how much storage am I using?"

### Phase 2 — Tailscale module
- `nix/modules/tailscale.nix` — tailscaled daemon, firewall rules, `tailscale-autoconnect` service
- `mkContainer.nix` injects `TAILSCALE_AUTH_KEY` env var and grants `/dev/net/tun` device access
- `ContainerSpec.tailscale_auth_key` flows through generator into Nix spec
- Agent reads auth key from `VoxnixSettings.tailscale_auth_key` when tailscale module requested

### Phase 3 — Per-user ZFS quotas
- `VoxnixSettings.zfs_user_quota` (default `10G`, env var `ZFS_USER_QUOTA`)
- Applied idempotently on every `create_user_datasets` call

### Deployment / networking fixes
- `source /etc/set-environment` added to `tailscale-autoconnect` script
- `Type=simple` in `tailscale-autoconnect` — avoids blocking `multi-user.target`
- `br-vox` outbound bridge added to host — containers get real IP and default route
- `hostBridge = "br-vox"` in `mkContainer.nix` with `networking.interfaces.eth0.useDHCP = true`
- Polling loop replaces `sleep 2` in `tailscale-autoconnect`

### This session — ZFS pool config (#68) and Tailscale logout on destroy (#60)

- **#68 — ZFS pool name as config:**
  - Added `zfs_pool: str = "tank"` field to `VoxnixSettings` in `agent/config.py` (env var `ZFS_POOL`)
  - Replaced module-level `_POOL`/`_USERS_ROOT`/`_MOUNT_ROOT` constants in `agent/tools/zfs.py` with lazy helper functions `_pool()`/`_users_root()`/`_mount_root()` that call `get_settings()` at call time — allows pool rename via env var without code changes
  - Injected `ZFS_POOL = "tank"` into agent service environment in `nix/host/agent-service.nix` — Nix config is now single source of truth for pool name
  - Updated `_mock_settings()` in `test_zfs.py` to include `zfs_pool` so path helper tests work through the mock
  - Fixed two hardcoded string assertions in `TestPathHelpers` to use `DEFAULT_POOL` constant

- **#60 — Tailscale cleanup on destroy + `--reset`:**
  - Added `_tailscale_logout()` helper in `agent/tools/containers.py` — runs `nixos-container run <name> -- tailscale logout` before `extra-container destroy`, cleanly removing the node from the tailnet (prevents ghost entries accumulating on repeated create/destroy)
  - Logout is best-effort: failure (container stopped, no Tailscale, control plane unreachable) is logged at debug/info level and does NOT abort the destroy
  - Added `--reset` flag to `tailscale up` in `nix/modules/tailscale.nix` — ensures clean re-enrollment when an auth key is rotated or a container is recreated with the same name
  - Added `_cmd_dispatch()` helper to `test_containers.py` for command-name-based dispatch (avoids fragile ordered `side_effect` sequences per #74)
  - Added 3 new tests: logout is called, logout failure doesn't abort destroy, logout failure not logged as error

### Previous session — code quality and bug fixes
- **Quota failure bug:** `create_user_datasets` was returning `success=True` even when quota application failed — now correctly returns `success=False` in both branches (existing dataset and new dataset)
- **ZFS refactor:** Extracted `_ensure_dataset(dataset, mountpoint)` helper — replaces the manual check-then-create pattern repeated 3× in `create_container_dataset`. ~80 lines → ~15 lines.
- **`_user_mount_path(owner)`** helper added to complete the path helper family
- **`_MOUNT_ROOT` constant** — `/tank/users` is now derived from `_POOL` (`_MOUNT_ROOT = f"/{_USERS_ROOT}"`). No more hardcoded strings.
- **CodeRabbit findings (second review):**
  - `_ensure_dataset` TOCTOU: treat "already exists" error from `zfs create` as success
  - `tailscale-autoconnect`: added `Restart=on-failure` + `RestartSec=15` — transient enrollment failures now retry instead of leaving container permanently disconnected
  - `mkContainer.nix` `hasWorkspace`: added non-empty string guard (`!= ""`) consistent with `hasTailscaleKey`
  - `create_container_dataset` docstring: removed stale `-p` flag reference

### Previous session — new issues filed
- **#67** — Decouple Telegram bot from agent via A2A protocol (`agent.to_a2a()` / fasta2a)
- **#68** — Surface ZFS pool name as config (`VoxnixSettings.zfs_pool`) rather than hardcoded constant
- **#69** — opencode session-aware PR triage via opencode server + Tailscale
- **#70** — Voice message support (Telegram audio → Whisper STT → agent)
- **#71** — Module self-description: agent can explain what each module does
- **#72** — Tiered Tailscale connectivity model (none / host tailnet / custom tailnet per user)
- **#73** — Document fragility of `install_succeeded` heuristic in `create_container`
- **#74** — Brittle ordered AsyncMock sequences in `test_zfs.py`

---

## Architecture decisions made this session (previous session)

### A2A modularization (#67)
Decouple Telegram bot (thin A2A client) from the container agent (A2A server via `agent.to_a2a()`). The Telegram layer becomes provider-agnostic — any A2A-compliant agent can be plugged in. PydanticAI supports this natively via `fasta2a`. `contextId` in A2A maps to `chat_id`, giving conversation history (#48) partially for free.

### Tiered Tailscale (#72)
Three-tier model: no Tailscale (br-vox only) / host tailnet (current `tailscale` module) / custom tailnet (`tailscale-custom` — user supplies their own auth key). Host subnet routing is explicitly NOT the right approach — it loses named nodes and per-container identity. Host Tailscale (#17) is for appliance admin access only, orthogonal to container connectivity.

### Module self-description (#71)
`discover_modules()` currently returns `list[str]` (names only). Should return `list[dict]` with `name` + `description`. Descriptions live in the Nix module (`meta.description`) — single source of truth, no drift risk. System prompt injection becomes richer: agent can explain what each module does when asked.

### Voice messages (#70)
Telegram voice messages arrive as OGG/Opus on `update.effective_message.voice`. Whisper API (OpenAI) is the simplest transcription path — same API key already in agenix. New `agent/chat/transcribe.py` module wraps the STT call. `handle_voice` handler added to `handlers.py`, registered with `filters.VOICE`.

---

## Known issue: stale Tailscale nodes on destroy/recreate — FIXED (#60)

~~When a container is destroyed and recreated with the same name, the old Tailscale node is NOT cleaned up. Each creation adds a new ghost entry in the Tailscale admin console.~~

**Fixed in this session (#60):** `_tailscale_logout()` in `agent/tools/containers.py` now runs `nixos-container run <name> -- tailscale logout` before every `extra-container destroy`. The logout is best-effort — it will silently skip (logged at debug) if the container is stopped or not enrolled. The `--reset` flag was also added to `tailscale up` in `tailscale.nix` to handle auth key rotation cleanly.

---

## Networking reference

### Host bridge setup (deployed)
```
br-vox:  10.100.0.1/24   — shared outbound bridge for all containers
dnsmasq: 10.100.0.100–200 DHCP range, 12h lease
NAT:     br-vox → eth0   (iptables MASQUERADE via nixos-nat-post chain)
```

### Container networking
- Each container gets `eth0` (veth attached to `br-vox` via `vb-<name>` on host side)
- Container gets IP from dnsmasq (typically `10.100.0.101` for first container)
- Default gateway: `10.100.0.1`
- After Tailscale enrolls, `tailscale0` appears with the Tailscale IP

### Tailscale node expiry
Tagged devices (`tag:shared`) have **key expiry disabled by default** in Tailscale. tailscaled reconnects automatically on container restart using stored state in `/var/lib/tailscale/tailscaled.state`. `tailscale-autoconnect` now has `Restart=on-failure` so transient enrollment failures are retried.

---

## What to work on next (priority order)

### 1. Conversation history + session context (#48) — High
Agent has no memory within a conversation. Each message is stateless. Consider doing #67 (A2A) first — A2A `contextId` gives per-user conversation continuity partially for free via the storage layer.

### 2. A2A modularization (#67) — Architectural, 2-3 sessions
Decouple Telegram bot from the agent via fasta2a. `agent.to_a2a()` exposes the container agent as an A2A server. Telegram becomes a thin A2A client. Prerequisite for multi-agent routing (#66, #29).

### 3. Diagnostic tools for the agent (#47) — High
Agent can't self-diagnose. `journalctl -M <name>`, `tailscale status`, `machinectl list` etc. as agent tools. High value — would have saved significant debugging time during deployment.

### 4. Container query tool (#54) — High
Users can create/destroy/start/stop but `list_workloads` exists (`tool_list_workloads`). Issue #54 is about *deeper* metadata — "tell me about the dev container" (modules, status, Tailscale IP, storage usage). `list_workloads` covers basic listing; #54 covers per-container detail.

### 5. Host Tailscale (#17) — Medium
Add `services.tailscale.enable = true` to host NixOS config. Enables out-of-LAN `just deploy` and SSH break-glass. Orthogonal to container Tailscale (#72).

### 6. GitHub Deployment Action (#53) — Medium
Deploy on merge to main via GitHub Actions. Removes local-machine LAN dependency.

---

## Open issues summary

| # | Title | Priority |
|---|-------|----------|
| #75 | Notifications / global agent formation | Low/idea |
| #74 | Brittle ordered AsyncMock sequences in test_zfs.py | Low (partially addressed: _cmd_dispatch added) |
| #73 | Document install-detection heuristic fragility | Low |
| #72 | Tiered Tailscale connectivity model | Medium |
| #71 | Module self-description | Medium |
| #70 | Voice message support | Medium |
| #69 | opencode session-aware PR triage | Low/idea |
| #68 | Surface ZFS pool name as config | **Done — merged PR #77** |
| #67 | A2A modularization (Telegram ↔ agent) | High |
| #66 | opencode Telegram wrapper | Low |
| #65 | Git worktree support | Low |
| #64 | BYOM (bring your own modules) | Low |
| #63 | iOS/Android native app | Low/idea |
| #62 | TTL-based multi-turn conversation | Medium |
| #61 | Zero trust auth layer for Telegram | Medium |
| #60 | Stale Tailscale node cleanup + --reset flag | **Done — merged PR #77** |
| #59 | Use spec.model_copy() to avoid mutating ContainerSpec | Low |
| #58 | Add Pydantic validator for zfs_user_quota format | Low |
| #57 | Consolidate validate_container_name into ContainerSpec | Low |
| #55 | iOS/Android widget display | Low/idea |
| #54 | Agent container "query" (deep metadata) | High |
| #53 | GitHub Deployment Action | Medium |
| #51 | Automated LLM quality evals | Low |
| #48 | Conversation history + session context | High |
| #47 | Expose diagnostic tools to agent | High |
| #34 | Configurable observability backend | Low |
| #32 | Custom installer ISO | Low/idea |
| #31 | Agent-driven evals | Low/idea |
| #30 | Telegram as mobile coding IDE | Low/idea |
| #29 | Opinionated coding agent workflow | Low/idea |
| #28 | First-boot web wizard | Low/idea |
| #27 | Streamline deployment | Medium |
| #26 | Additional server | Low/idea |
| #25 | Agent modularity / A2A (superseded by #67) | Low |
| #24 | Migrate agent packaging to pure Nix | Low |
| #23 | CI/provision check for placeholder SSH key | Low |
| #22 | Parameterize hardcoded values in host config | Low |
| #20 | Allow metadata pass-through | Low |
| #17 | Tailscale on appliance host | Medium |
| #15 | PowerShell convenience scripts | Low |
| #11 | VM exclusion test coverage | Low |
| #10 | Document VM exclusion behaviour | Low |
| #9  | warnings.warn stacklevel | Minor |
| #6  | Explicit @pytest.mark.asyncio markers | Low |

---

## Key debugging commands

```bash
# Agent logs (live, filter polling noise)
ssh admin@192.168.8.146 "journalctl -u voxnix-agent -f | grep -v getUpdates"

# Container status
ssh admin@192.168.8.146 "sudo machinectl list"
ssh admin@192.168.8.146 "sudo extra-container list"

# Tailscale status inside container
ssh admin@192.168.8.146 "sudo nsenter -t \$(sudo machinectl show dev --property=Leader --value) -m -p -- systemctl status tailscaled"

# Tailscale autoconnect logs
ssh admin@192.168.8.146 "sudo journalctl -M dev -u tailscale-autoconnect --no-pager -n 20"

# Container network
ssh admin@192.168.8.146 "LEADER=\$(sudo machinectl show dev --property=Leader --value) && sudo nsenter -t \$LEADER -n -- ip addr show eth0 && sudo nsenter -t \$LEADER -n -- ip route show"

# ZFS dataset state
ssh admin@192.168.8.146 "sudo zfs list -r tank/users"

# Full cleanup (container + ZFS)
ssh admin@192.168.8.146 "sudo extra-container destroy dev && sudo zfs destroy -r tank/users/8586298950/containers/dev"

# Bridge and NAT health
ssh admin@192.168.8.146 "ip addr show br-vox && systemctl is-active dnsmasq"
ssh admin@192.168.8.146 "sudo iptables -t nat -L nixos-nat-pre -n -v | grep br-vox"
```

---

## Appliance quick reference

| Path / Command | Purpose |
|---|---|
| `/var/lib/voxnix-agent/.venv` | Python virtualenv |
| `/var/lib/voxnix-agent/cache` | Nix eval + cache (`XDG_CACHE_HOME`) |
| `/var/lib/voxnix-agent/uv-cache` | uv download cache |
| `/run/agenix/agent-env` | Decrypted secrets (tmpfs — gone on reboot) |
| `/etc/nixos-containers/` | Per-container conf files |
| `/var/lib/nixos-containers/` | Container rootfs directories |
| `/tank/users/<chat_id>/containers/<name>/workspace` | Per-container ZFS workspace |
| `journalctl -u voxnix-agent -n 50 --no-pager` | Agent logs |
| `sudo machinectl list` | Running containers |
| `sudo extra-container list` | All containers (running + stopped) |
| `sudo extra-container destroy <name>` | Destroy container (use this, not `nixos-container destroy`) |
| `sudo systemctl show voxnix-agent --property=WorkingDirectory,Environment` | Resolve agent's env vars |
| `sudo systemctl show container@<name> --property=ActiveState,NRestarts` | Check if container is stable |

## ZFS constant reference

```python
# agent/tools/zfs.py — pool name now comes from VoxnixSettings (#68 fixed)
# _pool()        → get_settings().zfs_pool   (env var ZFS_POOL, default "tank")
# _users_root()  → f"{_pool()}/users"        # dataset paths: tank/users/...
# _mount_root()  → f"/{_users_root()}"       # mount paths:  /tank/users/...
```

Pool name is now driven by the `ZFS_POOL` env var (default `"tank"`). The Nix host config (`nix/host/agent-service.nix`) injects `ZFS_POOL = "tank"` — it is the single source of truth. Changing the pool only requires updating that one line in Nix config.