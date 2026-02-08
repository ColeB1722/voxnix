# voxnix

> Talk to your infrastructure.

An agentic NixOS container and VM orchestrator. Manage dev environments through natural language via Telegram — powered by NixOS-native containers, composable modules, and an AI agent as the control plane.

## What is this?

A self-hosted NixOS appliance where an AI agent orchestrates containers and VMs on your behalf. Instead of clicking through a web UI or writing YAML, you message the agent in Telegram: *"spin up a dev container with git, fish, and code-server"* — and it happens.

**Target audience:** Self-hosted on personal hardware, shared with friends and family.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architectural design, including:

- Workload strategy (NixOS containers + microvm.nix VMs)
- Trust model and multi-tenancy
- Networking model (Tailscale per-container, Pangolin for public access)
- Persistence (ZFS-backed, ephemeral systems)
- Implementation details (PydanticAI, JSON spec to Nix module composition)
- Development approach and tooling

## Status

Early development. The foundational MVP is in progress. See [Foundational MVP](docs/architecture.md#foundational-mvp) for scope.

## Development

```bash
# Enter dev environment
nix develop

# Or with direnv
direnv allow

# Task runner
just dev
just fmt
just check
```

## License

[MIT](LICENSE)
