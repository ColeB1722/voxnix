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
    pytest agent/

# Build the full appliance
build:
    nix build .#appliance

# Deploy to appliance
deploy target:
    nixos-rebuild switch --flake .#appliance --target-host admin@{{ target }} --use-remote-sudo
