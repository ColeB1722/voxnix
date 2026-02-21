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
    nix build .#appliance

# Deploy to appliance
deploy target:
    nixos-rebuild switch --flake .#appliance --target-host admin@{{ target }} --use-remote-sudo
