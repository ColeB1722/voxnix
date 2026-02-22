# Voxnix — Session Handoff

You are working on **voxnix** — an agentic NixOS container orchestrator. A Telegram bot backed by a PydanticAI agent manages NixOS containers on a self-hosted Hyper-V VM appliance. The agent generates JSON specs → Nix functions compose modules → `extra-container` builds and starts containers.

**Read `docs/architecture.md` and `AGENTS.md` before doing anything.**

---

## Current state

**Branch:** `fix/logfire-no-token` — open as PR #45. Not yet merged. Contains ~20 commits from the first real deployment session. Do not open new PRs until this branch is closed out.

**The appliance is provisioned and running** at `192.168.8.146` (Hyper-V Gen 2 VM on Windows 11 Pro, 16GB RAM). Agent service is active and the Telegram bot responds.

**What works:**
- `/start`, `/help`, free-form LLM responses (openrouter:anthropic/claude-haiku-4.5)
- Module discovery (git, fish, workspace)
- Container creation verified via root debug script on the appliance

**What needs confirming (#50):**
- End-to-end container creation via the Telegram bot
- Container destroy/start/stop/list via bot (untested on real hardware)

---

## Immediate task

**Verify E2E container creation through the bot, then close out PR #45.**

### Step 1 — Clean up test containers (#49)

```bash
ssh admin@192.168.8.146 "sudo extra-container destroy nstest testbox"
```

### Step 2 — Test container creation via bot

Ask the bot: "create a dev container with git and fish"

Expected: typing bubble for ~1-2 minutes (first build downloads ~17 derivations), then ✅ message. Container names must be ≤11 characters.

Verify on appliance:

```bash
ssh admin@192.168.8.146 "sudo machinectl list"
```

### Step 3 — Test remaining operations

Test via bot: destroy, start, stop, list workloads.

### Step 4 — CodeRabbit review and merge

```bash
~/.local/bin/coderabbit review --type committed --base main --plain
```

Triage findings per AGENTS.md protocol, then merge PR #45.

---

## Key learnings from deployment session (all documented in AGENTS.md)

- **Branch discipline:** don't open PRs or merge during debug sessions — accumulate fixes on one branch, deploy from branch tip, open PR when stable
- **SSH-first debugging:** reproduce failing commands directly on appliance; use `systemctl show voxnix-agent --property=Environment` to get exact service env
- **`ProtectSystem=strict` interactions:** any path a subprocess writes to must be in `ReadWritePaths`. Surprises encountered: `/tmp`, `/var/tmp`, `/etc/systemd-mutable`, `/root/.cache/nix` (redirect via `HOME=`)
- **Nix inside the service:** requires `HOME=/var/lib/voxnix-agent`, `NIX_PATH=nixpkgs=${inputs.nixpkgs}`, `XDG_CACHE_HOME=/var/lib/voxnix-agent/cache`
- **extra-container requirements on the host:** `boot.enableContainers = true`, `boot.extraSystemdUnitPaths = ["/etc/systemd-mutable/system"]`, container names ≤11 chars (privateNetwork interface limit)
- **Debugging pattern:** write a Python debug script replicating the service env, run as root on the appliance to reproduce issues without waiting for bot interactions

---

## Open issues — near-term

| # | Title | Priority |
|---|---|---|
| #50 | Verify E2E container creation via Telegram | **Immediate** |
| #49 | Clean up test containers | Quick |
| #46 | Handle Markdown formatting in agent responses | High — visible UX issue |
| #47 | Expose diagnostic tools to agent for self-diagnosis | High |
| #12 | Name validation on destroy/start/stop container tools | Bug |
| #48 | Conversation history + model hot-swap | Enhancement |
| #22 | Parameterize hardcoded host config values | Enhancement |
| #17 | Tailscale on the appliance host | Enhancement |

---

## Deployment commands (from ~/repos/voxnix in nix develop)

```bash
just deploy 192.168.8.146          # deploy current branch to appliance
just secrets-edit                  # edit encrypted secrets (agenix)
just secrets-rekey                 # rekey after adding appliance key
ssh admin@192.168.8.146 "..."      # direct appliance access (key-based)
```

## Appliance quick reference

| Path / Command | Purpose |
|---|---|
| `/var/lib/voxnix-agent/.venv` | Python virtualenv |
| `/var/lib/voxnix-agent/cache` | Nix eval + Nix cache (HOME redirect) |
| `/run/agenix/agent-env` | Decrypted secrets (tmpfs — gone on reboot) |
| `/etc/systemd-mutable/system` | extra-container dynamic unit installation |
| `journalctl -u voxnix-agent -n 50 --no-pager` | Agent logs |
| `journalctl -u voxnix-agent -f` | Live log tail |
| `sudo machinectl list` | Running containers |
| `sudo extra-container destroy <name>` | Destroy a container |
| `systemctl show voxnix-agent --property=Environment` | Full service environment |