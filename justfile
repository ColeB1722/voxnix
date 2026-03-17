# voxnix task runner

set dotenv-load := false
set shell := ["bash", "-euo", "pipefail", "-c"]

# List available recipes
default:
    @just --list

# ── Dev shell ────────────────────────────────────────────────────────────────

# Enter dev shell (direnv handles this automatically)
dev:
    nix develop

# ── Code quality ─────────────────────────────────────────────────────────────

# Format all files (Nix + Python)
fmt:
    treefmt

# Check formatting without modifying
check:
    treefmt --ci
    nix flake check

# Lint Python code
lint:
    uv run ruff check agent/

# Type-check Python code
typecheck:
    uv run ty check agent/

# Run Python tests (excludes evals — those require API keys)
test:
    uv run pytest agent/

# Lint + typecheck + test (local CI gate)
ci: lint typecheck test

# ── Local dev ────────────────────────────────────────────────────────────────

# Run LLM evals (secrets from secretspec eval profile — run `just secrets-check eval` first)
eval:
    secretspec run --profile eval -- uv run pytest -m evals -o "addopts=" agent/evals/ -x --tb=short

# Run the Telegram bot locally (secrets from secretspec bot profile — run `just secrets-check bot` first)
bot:
    secretspec run --profile bot -- uv run python -m agent.chat

# Verify secretspec secrets are configured for a profile (usage: just secrets-check eval)
secrets-check profile:
    secretspec check --profile {{ profile }}

# ── Secrets (agenix — appliance production secrets) ──────────────────────────

# Edit the encrypted secrets file
secrets-edit:
    cd secrets && agenix -e agent-env.age --identity ~/.config/age/voxnix.txt

# Rekey secrets after adding the appliance SSH key to secrets/secrets.nix
secrets-rekey:
    cd secrets && agenix --rekey --identity ~/.config/age/voxnix.txt

# ── Appliance ────────────────────────────────────────────────────────────────

# Build the full appliance configuration locally
build:
    nix build .#nixosConfigurations.appliance.config.system.build.toplevel

# Deploy to appliance (with SSH pre-flight check)
deploy target:
    @echo "Checking SSH connectivity to admin@{{ target }}..."
    @ssh -q -o ConnectTimeout=5 -o BatchMode=yes admin@{{ target }} exit 2>/dev/null || \
        (echo "Cannot reach admin@{{ target }} — is the appliance running?"; exit 1)
    @echo "SSH OK — starting rebuild..."
    nix run nixpkgs#nixos-rebuild -- switch --flake .#appliance --target-host admin@{{ target }} --build-host admin@{{ target }} --sudo

# Open an SSH session to the appliance
ssh target:
    ssh admin@{{ target }}

# Tail the voxnix agent log on the appliance (ctrl-c to stop)
logs target:
    ssh admin@{{ target }} "journalctl -u voxnix-agent -f --no-pager | grep -v getUpdates"

# Check voxnix agent status on the appliance
status target:
    ssh admin@{{ target }} "systemctl status voxnix-agent --no-pager"

# Restart the voxnix agent on the appliance
restart target:
    ssh admin@{{ target }} "sudo systemctl restart voxnix-agent"

# Provision a new appliance (one-time, destructive — formats the target disk)
provision target:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "WARNING: This will FORMAT THE DISK on {{ target }}. All existing data will be destroyed."
    echo "  Target: root@{{ target }}"
    echo ""
    read -p "Type the target IP to confirm: " confirm
    if [ "$confirm" != "{{ target }}" ]; then
        echo "Aborted."
        exit 1
    fi
    echo ""
    echo "Starting provisioning — nixos-anywhere will prompt for the root password..."
    nixos-anywhere --flake .#appliance root@{{ target }}
