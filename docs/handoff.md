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
**Current branch:** `feat/agent-improvements` — PR #84 open, pending human review
**Last known good container:** `dev` running, Tailscale enrolled at `100.83.13.65` (this IP changes on re-enrollment)
**PR #84 NOT YET DEPLOYED** — merge + deploy needed to validate new features on the appliance

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

## What was built (cumulative)

### This session — PR #84 (feat/agent-improvements, pending review)

Implements #48, #62, #47, #54, #81. Agent goes from stateless command executor → conversational infrastructure assistant with self-diagnosis.

**Conversation history (#48, #62):**
- `agent/chat/history.py` — `ConversationStore`: per-chat_id message history with 30-minute TTL
- PydanticAI `message_history` threaded through `agent.run()` → returns `(output, new_messages)` tuple
- `handle_message` retrieves/stores history per chat_id via `application.bot_data`
- History NOT stored on agent exceptions (no broken partial state)
- Memory safety cap: 200 messages per chat (store), 40 messages sent to LLM (history_processor)
- Context window trimming delegated to PydanticAI's `history_processors` pipeline (`keep_recent_turns` in agent.py) — clean separation from storage layer

**Diagnostic tools (#47) — 6 new agent tools:**
- `tool_check_host_health` — checklist: extra-container, machinectl, container@.service, ZFS
- `tool_get_container_logs` — journalctl with machine journal → host journal fallback
- `tool_get_container_status` — machinectl status → systemctl status fallback
- `tool_get_tailscale_status` — tailscale status inside a container
- `tool_check_service` — host systemd service status (allowlisted: voxnix-agent, tailscaled, nix-daemon, systemd-machined, systemd-networkd, sshd)
- All read-only CLI wrappers with structured `DiagnosticResult`, ownership-scoped where applicable

**Container query (#54) — `tool_query_container`:**
- `agent/tools/query.py` — deep metadata retrieval with parallel fan-out
- Installed modules (from `VOXNIX_MODULES` env var, Nix store fallback for stopped containers)
- Tailscale IP and hostname, ZFS workspace storage usage, uptime, owner verification
- Graceful degradation: partial facet failures still return available info
- `nix/mkContainer.nix` — added `VOXNIX_MODULES` environment variable for introspection

**Observability signal (#81):**
- Logfire warning in `containers.py` when creation fails with non-empty stdout but missing `"Installing containers:"` sentinel — surfaces heuristic drift before data loss

**Tests:** 303 → 388 (+85 new). Zero lint errors, zero type checker diagnostics.

**CodeRabbit:** Reviewed, 2 findings fixed (dead code in query.py, stale docstring in diagnostics.py).

### Previous sessions (all merged to main)

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

### PR #77 session — ZFS pool config (#68) and Tailscale logout on destroy (#60)

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

## Architecture decisions made this session

### Conversation history lives in-memory, not Telegram (#48, #88)
Telegram stores messages server-side but the bot never reads that history back. Our `ConversationStore` is the sole source of conversation context the agent sees. This is intentional — it's simple, lost on restart (acceptable for infra commands), and easy to migrate when A2A (#67) provides `contextId`-based storage. Decision on post-A2A storage location tracked in #88.

### Context window trimming via PydanticAI history_processors, not the store
The store accumulates full raw history (capped at 200 messages for memory safety). A `keep_recent_turns` history_processor on the Agent trims to 40 messages before every model request. This aligns with PydanticAI's built-in pipeline rather than reimplementing trimming in the store layer. Future summarization (#31-style) would be another processor — the store doesn't change.

### 12 tools is within safe range
Agent went from 6 → 12 tools. Under 20 is well within what models handle reliably. Diagnostic tools have overlapping semantics (query vs status vs logs) — evals (#51) will validate the agent picks the right one. A2A split (#67) will naturally partition tools across agents when tool count grows further.

## Architecture decisions made in previous sessions

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

### 0. Deploy + validate PR #84 — Immediate
PR #84 is undeployed. Merge, deploy to the appliance, test conversation history and new tools live via Telegram. Will surface integration issues mocks can't catch (systemd namespace visibility, VOXNIX_MODULES propagation, Tailscale query timing).

### 1. Agent evals (#51) — High (revised up)
Agent went from 6 → 12 tools with conversation history. The decision surface doubled. Without evals, we don't know if the agent reliably picks `tool_query_container` vs `tool_get_container_status` vs `tool_list_workloads` for ambiguous requests. Lightweight first pass: 10-15 synthetic conversations, deterministic assertions on tool selection, run against a fast model. Every future PR should include eval cases alongside features.

### 2. Host Tailscale (#17) — Medium
Add `services.tailscale.enable = true` to host NixOS config. Enables out-of-LAN `just deploy` and SSH break-glass. Small, orthogonal to everything.

### 3. GitHub Deployment Action (#53) — Medium
Deploy on merge to main via GitHub Actions. Combined with #17, removes LAN dependency.

### 4. Quick cleanup batch — Low effort
- #57 — Consolidate `validate_container_name` into `ContainerSpec`
- #83 — Replace stdout-parsing install detection with conf-file check
- #22 — Parameterize hardcoded values in host config

### 5. A2A modularization (#67) — Architectural, 2-3 sessions
Decouple Telegram bot from the agent via fasta2a. Do this WITH evals in place so you have a safety net. The conversation history decision (#88) becomes relevant here.

---

## Open issues summary

| # | Title | Priority |
|---|-------|----------|
| #88 | Decide where conversation history lives post-A2A | Architectural (deferred) |
| #86 | Fish shell module with local-optimized aliases + ttyd | Low/idea |
| #85 | Enforce PR approval via branch protection rules | Medium (ops) |
| #83 | Replace stdout-parsing install detection with conf-file check | Low |
| #81 | Observability signal for install heuristic mismatch | **Done — PR #84** |
| #80 | Browser-based terminal (ttyd) | Low/idea |
| #79 | Typing indicator | Low/idea |
| #78 | Fun idea | Low/idea |
| #75 | Notifications / global agent formation | Low/idea |
| #74 | Brittle ordered AsyncMock sequences in test_zfs.py | Low (partially addressed) |
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
| #62 | TTL-based multi-turn conversation | **Done — PR #84** |
| #61 | Zero trust auth layer for Telegram | Medium |
| #60 | Stale Tailscale node cleanup + --reset flag | **Done — merged PR #77** |
| #59 | Use spec.model_copy() to avoid mutating ContainerSpec | Low |
| #58 | Add Pydantic validator for zfs_user_quota format | Low |
| #57 | Consolidate validate_container_name into ContainerSpec | Low |
| #55 | iOS/Android widget display | Low/idea |
| #54 | Agent container "query" (deep metadata) | **Done — PR #84** |
| #53 | GitHub Deployment Action | Medium |
| #51 | Automated LLM quality evals | **High (revised up)** |
| #48 | Conversation history + session context | **Done — PR #84** |
| #47 | Expose diagnostic tools to agent | **Done — PR #84** |
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