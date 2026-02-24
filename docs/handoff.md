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
**Current branch:** `fix/deferred-cleanup` — **open PR #56, not yet merged**
**Last known good container:** `dev` running, Tailscale enrolled at `100.83.13.65` (this IP changes on re-enrollment)

---

## What is working end-to-end

All three E2E acceptance criteria verified this session:

1. **Container creation** — bot creates a NixOS container with git, fish, tailscale, workspace modules via `extra-container`
2. **Tailscale enrollment** — container appears in Tailscale admin console tagged `tag:shared`, online, with a stable IP
3. **SSH via Tailscale** — `ssh root@<tailscale-ip>` from the dev machine works (Tailscale SSH enabled via `--ssh` flag)
4. **Workspace persistence** — `/workspace` is a ZFS dataset that survives `stop`/`start` cycles

---

## What was built (cumulative, all on `fix/deferred-cleanup`)

### Phase 0 — Deferred fixes (#46, #52, #12)
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

### Deployment / networking fixes (this session)
- **`source /etc/set-environment`** added to `tailscale-autoconnect` script — systemd services don't source this automatically; `TAILSCALE_AUTH_KEY` was present but not visible to the service
- **`Type=simple` in `tailscale-autoconnect`** — NixOS implicitly adds `Before=multi-user.target` to units with `WantedBy=multi-user.target`. With `Type=oneshot`, nspawn couldn't send `sd_notify READY=1` until `tailscale up` completed, but the host `container@.service` has `TimeoutStartSec=1min`, causing a kill-and-restart loop every ~60 seconds. `Type=simple` makes the service "started" as soon as the process launches.
- **`br-vox` outbound bridge** added to host — `privateNetwork=true` without `hostBridge`/`localAddress` creates a fully isolated network namespace with no veth pair and no routes. Tailscale couldn't reach its control plane. Added bridge `br-vox` (10.100.0.1/24), dnsmasq DHCP (10.100.0.100–200), NAT masquerade from `br-vox → eth0`.
- **`hostBridge = "br-vox"` in `mkContainer.nix`** with `networking.interfaces.eth0.useDHCP = true` — containers now get a real IP and default route on boot.
- **Polling loop** replaces `sleep 2` in `tailscale-autoconnect` — waits up to 30s for tailscaled socket readiness.
- `isReadOnly = false` (not the non-existent `isReadWrite`) in `mkContainer.nix` bindMounts
- ZFS datasets created with explicit `mountpoint=<path>` at every level
- `create_container` preserves ZFS dataset when install succeeds but start fails (uses "Installing containers:" sentinel in stdout)
- `logfire.warn()` → `logfire.warning()` (correct API)
- ZFS: log warnings on silent mountpoint-set failures; return `ZfsResult` failure for intermediate dataset creation errors

---

## PR #56 — pending review and merge

**Branch:** `fix/deferred-cleanup`
**State:** pushed, open, not yet merged. Await manual review before merging.

**After merge:**
1. Delete the branch (squash merge, auto-deleted)
2. Update this file — change branch to `main`, update "what's next"

**CodeRabbit triage already posted** on PR #56. Issues filed for tracked findings: #57, #58, #59, #60.

---

## Known issue: stale Tailscale nodes on destroy/recreate

When a container is destroyed and recreated with the same name, the old Tailscale node entry is NOT cleaned up — it persists as a ghost in the Tailscale admin console. Each creation adds a new node.

**Fix (not yet implemented):** call `tailscale logout` (or `tailscale down --accept-risk=lose-ssh`) inside the container before `extra-container destroy` tears it down. In `destroy_container` in `agent/tools/containers.py`, add a step before the destroy call:

```python
await run_command("nixos-container", "run", name, "--", "tailscale", "logout")
```

This is tracked as part of issue #60 (related: `--reset` flag). Do this before the node accumulation becomes painful.

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
Tagged devices (`tag:shared`) have **key expiry disabled by default** in Tailscale. This is correct — tagged infrastructure nodes should not require periodic human re-authentication. tailscaled reconnects automatically on container restart using stored state in `/var/lib/tailscale/tailscaled.state`.

---

## What to work on next (priority order)

### 1. Stale Tailscale node cleanup (1 session, unblocked)
Add `tailscale logout` before destroy in `agent/tools/containers.py`. Fixes ghost nodes accumulating in the tailnet on repeated create/destroy. Do this soon — test containers already left stale entries.

### 2. Conversation history + session context (#48)
The agent has no memory within a conversation. "Stop container dev" works, "now destroy it" doesn't — no referent. This is the biggest day-to-day UX gap and is also a prerequisite for confirm-before-destroy on destructive operations. Tracked as #48.

### 3. Diagnostic tools for the agent (#47)
Agent can't self-diagnose. If Tailscale enrollment fails, users get a vague error. If the agent could query `journalctl -M <name>`, `tailscale status`, `machinectl list` etc., it could answer "why is my container unhealthy?" without requiring the user to SSH in. High value — would have saved significant time this session. Tracked as #47.

### 4. Container query tool (#54)
Users can create/destroy/start/stop but can't ask "what containers do I have?" or "is dev running?". Tracked as #54.

### 5. GitHub Deployment Action (#53)
Deploy is currently manual (`just deploy <ip>`) from a machine on the same LAN. A GitHub Actions workflow that deploys on merge to main would remove the local-machine dependency. Tracked as #53.

---

## Open issues summary

| # | Title | Priority |
|---|-------|----------|
| #60 | Add `--reset` flag to tailscale up; stale node cleanup | High |
| #59 | Use `spec.model_copy()` to avoid mutating ContainerSpec | Low |
| #58 | Add Pydantic validator for `zfs_user_quota` format | Low |
| #57 | Consolidate `validate_container_name` into `ContainerSpec.validate_name` | Low |
| #54 | Add agent container "query" tool | High |
| #53 | GitHub Deployment Action | Medium |
| #51 | Automated LLM quality evals | Low |
| #48 | Conversation history + session context | High |
| #47 | Expose diagnostic tools to agent | High |
| #34 | Configurable observability backend | Low |
| #27 | Streamline deployment | Medium |
| #24 | Migrate agent packaging to pure Nix | Low |
| #11 | VM exclusion test coverage | Low |
| #10 | Document VM exclusion | Low |
| #9  | `warnings.warn` stacklevel | Minor |

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

# Container network (confirm bridge IP and routes)
ssh admin@192.168.8.146 "LEADER=\$(sudo machinectl show dev --property=Leader --value) && sudo nsenter -t \$LEADER -n -- ip addr show eth0 && sudo nsenter -t \$LEADER -n -- ip route show"

# ZFS dataset state
ssh admin@192.168.8.146 "sudo zfs list -r tank/users"

# Full cleanup (container + ZFS)
ssh admin@192.168.8.146 "sudo extra-container destroy dev && sudo zfs destroy -r tank/users/8586298950/containers/dev"

# Bridge and NAT health
ssh admin@192.168.8.146 "ip addr show br-vox && systemctl is-active dnsmasq"
ssh admin@192.168.8.146 "sudo iptables -t nat -L nixos-nat-pre -n -v | grep br-vox"

# Run the agent tools directly (bypass Telegram for debugging)
ssh admin@192.168.8.146 'sudo env \
  HOME=/var/lib/voxnix-agent \
  XDG_CACHE_HOME=/var/lib/voxnix-agent/cache \
  VOXNIX_FLAKE_PATH=$(sudo systemctl show voxnix-agent --property=Environment | grep -o "VOXNIX_FLAKE_PATH=[^ ]*" | cut -d= -f2) \
  $(sudo cat /run/agenix/agent-env | sed "s/export //") \
  LOGFIRE_IGNORE_NO_CONFIG=1 \
  PATH=<agent-service-PATH> \
  /var/lib/voxnix-agent/.venv/bin/python -c "..."'
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