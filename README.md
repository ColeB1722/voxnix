# voxnix

> Talk to your infrastructure.

An agentic NixOS container and VM orchestrator. Manage dev environments through natural language via Telegram — powered by NixOS-native containers, composable modules, and an AI agent as the control plane.

## What is this?

A self-hosted NixOS appliance where an AI agent orchestrates containers and VMs on your behalf. Instead of clicking through a web UI or writing YAML, you message the agent in Telegram: *"spin up a dev container with git, fish, and code-server"* — and it happens.

**Target audience:** Self-hosted on personal hardware, shared with friends and family.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full design, including workload strategy, trust model, networking, persistence, and implementation details.

## Status

Early development. The foundational MVP is in progress. See [Foundational MVP](docs/architecture.md#foundational-mvp) for scope.

---

## Deployment

### Hyper-V (Windows 11 Pro)

This is the primary documented deployment path. The appliance runs as a Hyper-V Generation 2 VM on a Windows 11 Pro host. Deployment is driven from a separate machine on the same LAN (a MacBook or any Linux/macOS machine with Nix installed).

**Reference hardware:** 32-core CPU, 32 GB RAM, NVMe SSD (OS/appliance) + SATA SSD (optional overflow). The VM uses dynamic memory so Hyper-V returns RAM to Windows when the appliance is idle or shut down — useful when reclaiming resources for other workloads.

#### Prerequisites

**On the Windows PC (once only):**
- Windows 11 Pro with Hyper-V enabled
  ```powershell
  # Run as Administrator if not already enabled
  Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
  ```

**On the deployment machine (MacBook or Linux):**
- [Nix](https://install.determinate.systems/) with flakes enabled
- `git`
- `ssh` with your admin key available

**Accounts and credentials (before provisioning):**
- A Telegram bot token — create one via [@BotFather](https://t.me/BotFather)
- An LLM provider API key (Anthropic, OpenAI, etc.)
- A [Logfire](https://logfire.pydantic.dev/) project token (optional — omit for local-only tracing)

---

#### Step 1 — Create the Hyper-V external switch (once only)

The appliance needs a real LAN IP so your deployment machine can reach it over SSH and so Tailscale can connect outbound. Use an **external switch** bridged to the NIC connected to your LAN.

Open **Hyper-V Manager → Virtual Switch Manager → New → External**, select your LAN network adapter, and name it `voxnix-lan` (or any name — you'll reference it in step 2).

---

#### Step 2 — Create the VM (once only)

Open **Hyper-V Manager → New → Virtual Machine**:

| Setting | Value |
|---|---|
| Generation | **2** (required for UEFI/Secure Boot) |
| Startup memory | 8192 MB |
| Dynamic memory | **Enabled** — min 2048 MB, max 16384 MB |
| Network | External switch created in step 1 |
| Virtual disk | New VHDX on your NVMe SSD, **60 GB** minimum |
| Installation media | [NixOS minimal ISO](https://nixos.org/download/) (x86_64-linux) |

After creation, open VM **Settings**:
- **Security → Secure Boot template:** change to `Microsoft UEFI Certificate Authority` (required for NixOS)
- **Processor:** assign 4–8 vCPUs (leave the rest for Windows/gaming)
- **Integration Services:** ensure **Guest services** and **Shutdown** are both checked — these allow graceful shutdown from Windows, which matters for clean ZFS state

> **Checkpoint tip:** After initial NixOS installation (step 4), take a Hyper-V checkpoint before provisioning. This gives you a clean rollback point if provisioning goes wrong.

---

#### Step 3 — Boot into the NixOS installer

Start the VM and attach to its console in Hyper-V Manager. Boot from the ISO.

Once at the installer shell, note the VM's IP address:

```bash
ip addr show eth0
```

Set a temporary root password so `nixos-anywhere` can connect:

```bash
passwd root
```

Leave the installer shell running.

---

#### Step 4 — Configure secrets with agenix (before provisioning)

All runtime credentials are managed by [agenix](https://github.com/ryantm/agenix) and injected into the agent's systemd service as a single environment file. Nothing is hardcoded.

**Generate your admin age key (once only, on your MacBook):**

```bash
age-keygen -o ~/.config/age/voxnix.txt
# prints: Public key: age1...
```

**Add your public key** to `secrets/secrets.nix` — replace the `admin` placeholder with the public key printed above.

**Add your SSH public key** to `nix/host/default.nix` — replace the `AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY` placeholder in `users.users.admin.openssh.authorizedKeys.keys`.

**Create the encrypted environment file:**

```bash
cd secrets
agenix -e agent-env.age
```

Your editor will open. Paste the following (with your real values):

```
TELEGRAM_BOT_TOKEN=1234567:ABCdefGHIjklMNOpqrSTUvwxYZ
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-api03-...
LOGFIRE_TOKEN=your-logfire-token
```

> **Notes:** The API key variable must match the provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.). `LOGFIRE_TOKEN` is optional — omit the line entirely to disable remote tracing.

Commit the encrypted `.age` file (safe to commit — only your age key can decrypt it):

```bash
git add secrets/agent-env.age
git commit -m "secrets: add encrypted agent environment"
```

See `secrets/README.md` for full details on secret management and rotation.

---

#### Step 5 — Provision with nixos-anywhere (from your MacBook)

Clone the repo (if you haven't already) and provision the appliance. This formats the disk via `disko`, installs NixOS, and applies the full appliance configuration in one shot.

```bash
git clone https://github.com/ColeB1722/voxnix.git
cd voxnix

just provision <vm-ip>
```

The `provision` recipe includes a destructive-action confirmation prompt — you must type the target IP to confirm. It also runs an SSH pre-flight check to verify the installer is reachable.

The VM will reboot into the fully configured appliance when complete. The temporary root password is gone — SSH access is now key-based only via the `admin` user.

**After provisioning — add the host key and rekey secrets:**

The appliance now has an SSH host key. Add it to `secrets/secrets.nix` so the appliance can decrypt secrets at boot:

```bash
ssh-keyscan <vm-ip> 2>/dev/null | grep ed25519
# Paste the full ssh-ed25519 line as the `host` value in secrets/secrets.nix

cd secrets && agenix --rekey
git add -A && git commit -m "secrets: rekey with host key"
just deploy <vm-ip>
```

The agent systemd service will start automatically with all secrets injected.

---

#### Step 6 — Verify

SSH into the appliance and check the agent service:

```bash
ssh admin@<vm-ip>
systemctl status voxnix-agent
journalctl -u voxnix-agent -f
```

Send `/start` to your Telegram bot. You should receive the welcome message.

> **Tip:** If the agent service is not running, check `journalctl -u voxnix-agent -e` — the most common cause is a missing or malformed `agent-env.age` (see step 4).

---

### Day-to-day operations

#### Deploy a config update (from MacBook)

Any change to the repo — new NixOS module, agent code update, host config tweak — is deployed with:

```bash
just deploy <vm-ip>
# runs SSH pre-flight check, then:
# nixos-rebuild switch --flake .#appliance --target-host admin@<vm-ip> --use-remote-sudo
```

The rebuild is atomic. If something breaks, roll back with:

```bash
ssh admin@<vm-ip> sudo nixos-rebuild switch --rollback
```

#### Reclaiming resources for gaming

Shut the appliance down gracefully from PowerShell on the Windows PC:

```powershell
Stop-VM -Name voxnix
```

This sends an ACPI shutdown signal via Hyper-V integration services, giving NixOS time to flush ZFS and stop services cleanly. Do not use **Turn Off** — that is equivalent to a hard power cut and bypasses clean shutdown.

Start it back up when you're done:

```powershell
Start-VM -Name voxnix
```

The agent service starts automatically on boot. Give it ~30 seconds, then message your bot to confirm it's back.

#### Check appliance status from PowerShell

```powershell
Get-VM -Name voxnix | Select-Object Name, State, MemoryAssigned, CPUUsage
```

#### SSH access (break-glass / maintenance)

Direct host access for debugging. LAN only — not exposed externally.

```bash
ssh admin@<vm-ip>
```

---

### Other deployment targets

The steps above are specific to Hyper-V. For other targets the Nix provisioning steps (4 onwards) are identical — only the VM/machine creation differs.

| Target | Notes |
|---|---|
| Bare metal | Skip steps 1–3; boot from NixOS USB, run `nixos-anywhere` the same way |
| VirtualBox | Generation 2 equivalent is EFI mode; use a bridged network adapter |
| Other hypervisors (Proxmox, VMware, etc.) | Use a bridged/routed network; ensure UEFI boot; install Hyper-V integration services equivalent if available |

---

## Development

```bash
# Enter dev environment
nix develop
# or with direnv
direnv allow

# Common tasks
just fmt        # format all files
just check      # format check + nix flake check
just lint       # ruff check agent/
just typecheck  # ty check agent/
just test       # uv run pytest agent/
just ci         # lint + typecheck + test
just bot        # run the Telegram bot locally (requires .env)
just deploy <ip>  # deploy to appliance
```

See `justfile` for all available recipes.

## License

[MIT](LICENSE)