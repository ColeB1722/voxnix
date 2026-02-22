# voxnix task runner

# Enter dev shell
dev:
    nix develop

# Format all files (Nix + Python + Markdown)
fmt:
    treefmt

# Check formatting without modifying
check:
    treefmt --ci
    nix flake check

# Run Python tests
test:
    uv run pytest agent/

# Run the Telegram bot
bot:
    uv run python -m agent.chat

# Lint Python code
lint:
    uv run ruff check agent/

# Type-check Python code
typecheck:
    uv run ty check agent/

# Lint + typecheck + test
ci: lint typecheck test

# Build the full appliance
build:
    nix build .#nixosConfigurations.appliance.config.system.build.toplevel

# Deploy to appliance (with SSH pre-flight check)
deploy target:
    @echo "Checking SSH connectivity to admin@{{ target }}..."
    @ssh -q -o ConnectTimeout=5 -o BatchMode=yes admin@{{ target }} exit 2>/dev/null || \
        (echo "❌ Cannot reach admin@{{ target }} — is the appliance running?"; exit 1)
    @echo "✅ SSH OK — starting rebuild..."
    nixos-rebuild switch --flake .#appliance --target-host admin@{{ target }} --use-remote-sudo

# Provision a new appliance (one-time, destructive — formats the target disk)
provision target:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "⚠️  This will FORMAT THE DISK on {{ target }}. All existing data will be destroyed."
    echo "   Target: root@{{ target }}"
    echo ""
    read -p "Type the target IP to confirm: " confirm
    if [ "$confirm" != "{{ target }}" ]; then
        echo "Aborted."
        exit 1
    fi
    echo ""
    echo "Starting provisioning — nixos-anywhere will prompt for the root password..."
    nixos-anywhere --flake .#appliance root@{{ target }}
