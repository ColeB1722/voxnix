# Voxnix — Session Handoff

> **This file is the persistent memory of the project across sessions.**
> Update it at the end of every session before stopping work.
> The next agent reads this before doing anything else.

You are working on **voxnix** — an agentic NixOS container orchestrator. A Telegram bot backed by a PydanticAI agent manages NixOS containers on a self-hosted Hyper-V VM appliance. The agent generates JSON specs → Nix functions compose modules → `extra-container` builds and starts containers.

**Always read `docs/architecture.md` and `AGENTS.md` before starting work.**
**Always run `gh issue list` before starting work to check open tracked debt.**

---

## System state

**Appliance:** `192.168.8.157` (Hyper-V Gen 2 VM, 16GB RAM) — ⚠️ IP rotates via DHCP, see #89
**Agent service:** active, Telegram bot responding
**Current branch:** `main` — PR #96 merged, PR for pipefail fix pending
**Last known good container:** `dev` running, Tailscale enrolled at `100.83.13.65` (this IP changes on re-enrollment)
**Eval CI:** running post-merge on main, but issue reporting was broken until pipefail fix (see this session notes)

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

### This session — PR #96 (feat/evals, merged) + pipefail fix (pending PR)

Implements #51. Full deepeval LLM quality eval suite with post-merge CI.

**Eval infrastructure (`agent/evals/`):**
- `scenarios.py` — `Scenario` / `MultiTurnScenario` dataclasses with `Metric(StrEnum)` for type-safe quality checks. 19 single-turn scenarios across 5 categories (query, diagnostics, lifecycle, storage, edge cases) + 2 multi-turn scenarios
- `helpers.py` — PydanticAI → deepeval bridge using `ModelResponse.tool_calls` (native property, not manual part iteration). `extract_tool_calls`, `extract_tool_names`, `build_test_case`
- `conftest.py` — single-model eval design: `LLM_PROVIDER`/`LLM_MODEL` default to `openrouter`/`anthropic/claude-haiku-4.5`. Judge model (gpt-4.1) auto-detects credentials from `OPENAI_API_KEY` or `OPENROUTER_API_KEY` (aliased). All CLI infrastructure fully mocked; LLM call is real
- `test_single_turn.py` — scenario-driven eval runner with `ToolCorrectnessMetric` + 6 `GEval` response quality metrics, tier-based enforcement (must → assert, should → warn)
- `test_multi_turn.py` — conversation context retention via deepeval `ConversationalTestCase` + `KnowledgeRetentionMetric` (informational, warn-only)
- `.github/workflows/eval.yml` — post-merge CI, auto-creates/updates `eval-regression` GitHub issue on failure, auto-closes on pass

**Key design decisions:**
- One model at a time (no cross-model parametrization) — simpler, cheaper
- CI only needs `OPENROUTER_API_KEY` — covers both test model and judge
- Evals excluded from default `pytest` runs via marker + `addopts`; run with `just eval`
- Scenarios are pure data — adding an eval means adding a `Scenario`, nothing else changes

**Bug found + fixed:** `eval.yml` pipeline (`pytest | tee eval-output.txt`) masked pytest's exit code — `tee` always returns 0, so `continue-on-error` never triggered, no regression issues were ever created, and status always reported "✅ All evals passed" even with failures. Fixed with `set -o pipefail`.

**Refactors during session:**
- Simplified provider auto-detection (removed 6-provider detection, cross-model infra)
- `helpers.py` — use `ModelResponse.tool_calls` property, drop `ToolCallPart` import, remove unused `extract_final_output`
- Metric names → `Metric(StrEnum)` with typed `QualityCheck.metric` field
- CodeRabbit findings fixed: `config.py` docstring provider examples, `test_multi_turn.py` stacklevel consistency

**Tests:** 388 passed, evals skipped without API keys. Zero lint errors.

### Previous session — PR #84 (feat/agent-improvements, merged)

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

### Eval design: single model, OpenRouter defaults, no cross-model parametrization
Simplified from the initial plan (which had 6-provider auto-detection and cross-model tests) to a focused single-model design:
- `LLM_PROVIDER`/`LLM_MODEL` default to `openrouter`/`anthropic/claude-haiku-4.5`
- Judge model (deepeval's gpt-4.1) auto-detects from `OPENAI_API_KEY` (native) or `OPENROUTER_API_KEY` (aliased via `OPENAI_BASE_URL`)
- CI needs exactly one secret (`OPENROUTER_API_KEY`) to auth both roles
- Cross-model parametrization removed — adds complexity for marginal value at this stage. Override `LLM_PROVIDER`/`LLM_MODEL` env vars to test a different model.

### Metric names as StrEnum
Bare string constants for metric names replaced with `Metric(StrEnum)`. `QualityCheck.metric` typed as `Metric` instead of `str`. Catches typos at definition time instead of runtime dict lookup. `StrEnum` is a `str` subclass so it's a drop-in replacement for dict keys, GEval `name=`, and f-strings.

### PydanticAI message helpers: use native properties
`ModelResponse.tool_calls` is a first-class property that already filters to `ToolCallPart` instances. The original `extract_tool_calls` manually iterated `msg.parts` with `isinstance` checks — unnecessary. `extract_final_output` was dead code (callers use `result.output` directly). Helpers now only exist for the PydanticAI → deepeval `ToolCall` format bridge.

### Previous session — Eval framework: deepeval, not hand-rolled pytest (#51)
Decision: deepeval over hand-rolled pytest. `ToolCorrectnessMetric` for tool selection, `GEval` for response quality with LLM-as-judge scoring. PydanticAI's `TestModel` is for integration tests (tool wiring), not evals. See PR #96 for the full implementation.

### Appliance IP rotates via DHCP (#89)
Host uses `networking.useDHCP = true` — IP changes on lease expiry or reboot (was `.146`, now `.157`). Three fix paths: static IP in NixOS config, DHCP reservation on router, or Host Tailscale (#17) making LAN IP irrelevant. Tracked in #89.

### Previous session — Conversation history lives in-memory, not Telegram (#48, #88)
Telegram stores messages server-side but the bot never reads that history back. Our `ConversationStore` is the sole source of conversation context the agent sees. This is intentional — it's simple, lost on restart (acceptable for infra commands), and easy to migrate when A2A (#67) provides `contextId`-based storage. Decision on post-A2A storage location tracked in #88.

### Previous session — Context window trimming via PydanticAI history_processors, not the store
The store accumulates full raw history (capped at 200 messages for memory safety). A `keep_recent_turns` history_processor on the Agent trims to 40 messages before every model request. This aligns with PydanticAI's built-in pipeline rather than reimplementing trimming in the store layer. Future summarization (#31-style) would be another processor — the store doesn't change.

### Previous session — 12 tools is within safe range
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

### 1. Fix eval baseline regressions — High (immediate)
The first real CI eval run (with pipefail fix) will surface the same 4 failures seen in the broken run. These are real agent quality issues, not eval bugs:
- **#92** — Agent uses Markdown formatting (NoMarkdown failures on `query-tell-me-about`, `diag-why-cant-create`, `edge-whats-happening`). Fix: strengthen system prompt anti-Markdown instructions.
- **#93** — Agent too verbose for simple ops (Conciseness failure on `diag-why-cant-create`). Fix: system prompt guidance on brevity.
- `status-is-running` — agent called `tool_list_workloads` instead of `tool_get_container_status`. Fix: tool docstring disambiguation or system prompt guidance.
- **#97** — Add MUST NOT scenarios for welcome-prompt and confirmation-prompt regressions (coverage gap from #51 spec).

### 2. Telegram access control (#61) — High (security)
Anyone who discovers the bot can currently create containers on the appliance. Add an allowlist of authorized Telegram chat IDs (env var or agenix secret). This is the single biggest security gap — ownership scoping is already wired through, but there's no gate on who can talk to the bot in the first place.

### 3. Eval coverage expansion — High
- **#94** — Multi-turn knowledge retention scores 0.00. Investigate whether message history threading in evals matches Telegram bot behavior, and whether KnowledgeRetentionMetric thresholds need tuning.
- **#97** — Add `NoWelcomePrompt` and `NoConfirmationPrompt` GEval metrics + scenarios.

### 4. Host Tailscale (#17) — Medium
Add `services.tailscale.enable = true` to host NixOS config. Enables out-of-LAN `just deploy` and SSH break-glass. Small, orthogonal to everything. Also mitigates #89 (DHCP IP rotation) by providing a stable Tailscale IP.

### 5. GitHub Deployment Action (#53) — Medium
Deploy on merge to main via GitHub Actions. Combined with #17, removes LAN dependency.

### 6. Quick cleanup batch — Low effort
- #57 — Consolidate `validate_container_name` into `ContainerSpec`
- #83 — Replace stdout-parsing install detection with conf-file check
- #22 — Parameterize hardcoded values in host config
- #89 — Fix appliance DHCP IP rotation (static IP or rely on #17)
- #95 — Pin DeterminateSystems CI actions to specific versions

### 7. A2A modularization (#67) — Architectural, 2-3 sessions
Decouple Telegram bot from the agent via fasta2a. Evals are now in place as a safety net. The conversation history decision (#88) becomes relevant here.

---

## Open issues summary

**Eval-related (new this session):**

| # | Title | Priority |
|---|-------|----------|
| #97 | eval: add MUST NOT scenarios (welcome-prompt, confirmation-prompt) | High |
| #95 | ops: pin DeterminateSystems CI actions to specific versions | Low |
| #94 | eval: multi-turn knowledge retention scores 0.00 on baseline | Medium |
| #93 | fix: agent responses too verbose for simple operations | High |
| #92 | fix: agent uses Markdown formatting (raw text in Telegram) | High |

**Infrastructure / ops:**

| # | Title | Priority |
|---|-------|----------|
| #91 | Evaluate Python project structure for multi-agent scalability | Medium |
| #61 | Telegram access control — allowlist authorized chat IDs | **High (security)** |
| #89 | Appliance IP rotates via DHCP — static IP or Host Tailscale | Medium (ops) |
| #88 | Decide where conversation history lives post-A2A | Architectural (deferred) |
| #85 | Enforce PR approval via branch protection rules | Medium (ops) |
| #83 | Replace stdout-parsing install detection with conf-file check | Low |
| #67 | A2A modularization (Telegram ↔ agent) | High |
| #57 | Consolidate validate_container_name into ContainerSpec | Low |
| #53 | GitHub Deployment Action | Medium |
| #27 | Streamline deployment | Medium |
| #24 | Migrate agent packaging to pure Nix | Low |
| #23 | CI/provision check for placeholder SSH key | Low |
| #22 | Parameterize hardcoded values in host config | Low |
| #17 | Tailscale on appliance host | Medium |

**Features / enhancements:**

| # | Title | Priority |
|---|-------|----------|
| #87 | Optimize for flexibility (startup wizard, auto-create tags) | Low/idea |
| #86 | Fish shell module with local-optimized aliases + ttyd | Low/idea |
| #80 | Browser-based terminal (ttyd) | Low/idea |
| #79 | Typing indicator | Low/idea |
| #78 | Fun idea | Low/idea |
| #75 | Notifications / global agent formation | Low/idea |
| #72 | Tiered Tailscale connectivity model | Medium |
| #71 | Module self-description | Medium |
| #70 | Voice message support | Medium |
| #69 | opencode session-aware PR triage | Low/idea |
| #66 | opencode Telegram wrapper | Low |
| #65 | Git worktree support | Low |
| #64 | BYOM (bring your own modules) | Low |
| #63 | iOS/Android native app | Low/idea |
| #61 | Zero trust auth layer for Telegram | Medium |
| #55 | iOS/Android widget display | Low/idea |
| #34 | Configurable observability backend | Low |
| #32 | Custom installer ISO | Low/idea |
| #31 | Agent-driven evals (natural language, web frontend) | Low/idea |
| #30 | Telegram as mobile coding IDE | Low/idea |
| #29 | Opinionated coding agent workflow | Low/idea |
| #28 | First-boot web wizard | Low/idea |
| #26 | Additional server | Low/idea |
| #25 | Agent modularity / A2A (superseded by #67) | Low |
| #20 | Allow metadata pass-through | Low |
| #15 | PowerShell convenience scripts | Low |

**Minor / cleanup:**

| # | Title | Priority |
|---|-------|----------|
| #11 | VM exclusion test coverage | Low |
| #10 | Document VM exclusion behaviour | Low |
| #9  | warnings.warn stacklevel | Minor |
| #6  | Explicit @pytest.mark.asyncio markers | Low |

**Closed this session:** #51 (automated LLM quality evals)
**Previously closed:** #48, #47, #54, #62, #81, #68, #60, #46, #52, #12

---

## Key debugging commands

```bash
# Agent logs (live, filter polling noise)
ssh admin@192.168.8.157 "journalctl -u voxnix-agent -f | grep -v getUpdates"

# Container status
ssh admin@192.168.8.157 "sudo machinectl list"
ssh admin@192.168.8.157 "sudo extra-container list"

# Tailscale status inside container
ssh admin@192.168.8.157 "sudo nsenter -t \$(sudo machinectl show dev --property=Leader --value) -m -p -- systemctl status tailscaled"

# Tailscale autoconnect logs
ssh admin@192.168.8.157 "sudo journalctl -M dev -u tailscale-autoconnect --no-pager -n 20"

# Container network
ssh admin@192.168.8.157 "LEADER=\$(sudo machinectl show dev --property=Leader --value) && sudo nsenter -t \$LEADER -n -- ip addr show eth0 && sudo nsenter -t \$LEADER -n -- ip route show"

# ZFS dataset state
ssh admin@192.168.8.157 "sudo zfs list -r tank/users"

# Full cleanup (container + ZFS)
ssh admin@192.168.8.157 "sudo extra-container destroy dev && sudo zfs destroy -r tank/users/8586298950/containers/dev"

# Bridge and NAT health
ssh admin@192.168.8.157 "ip addr show br-vox && systemctl is-active dnsmasq"
ssh admin@192.168.8.157 "sudo iptables -t nat -L nixos-nat-pre -n -v | grep br-vox"
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