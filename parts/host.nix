# NixOS appliance host configuration — wires up nixosConfigurations.appliance.
#
# This is the top-level flake-parts module that defines the full NixOS system
# consumed by:
#   - nixos-anywhere --flake .#appliance   (initial provisioning)
#   - nixos-rebuild --flake .#appliance    (ongoing updates)
#   - nix build .#nixosConfigurations.appliance.config.system.build.toplevel (CI)
#
# It composes:
#   - disko:   declarative disk partitioning (ZFS pool layout)
#   - agenix:  runtime secret decryption (Telegram token, LLM keys, etc.)
#   - nix/host/*:  hardware, networking, storage, secrets, agent service
#   - voxnix-src:  the repo source bundled as a store path (from parts/agent.nix)
#
# See docs/architecture.md § Deployment Workflow and § Foundational MVP.
{ inputs, self, ... }:
{
  # nixosConfigurations is a top-level flake output (not per-system), so we
  # define it under `flake` rather than `perSystem`.
  flake.nixosConfigurations.appliance = inputs.nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";

    # specialArgs makes these values available as module arguments in all
    # NixOS modules imported below (e.g. nix/host/agent-service.nix receives
    # `voxnix-src` as a function argument).
    specialArgs = {
      inherit inputs;

      # The full repo source as a /nix/store path. Used by:
      #   - agent-service.nix: VOXNIX_FLAKE_PATH + WorkingDirectory
      #   - The agent at runtime: locates nix/mkContainer.nix for expression generation
      #
      # Updated atomically on every nixos-rebuild — the agent always sees the
      # same module library and Nix code that the host was built with.
      voxnix-src = self.packages.x86_64-linux.voxnix-src;
    };

    modules = [
      # ── Disk partitioning ────────────────────────────────────────────────
      #
      # disko's NixOS module reads the `disko.devices` option set in
      # nix/host/storage.nix and generates the corresponding fstab entries,
      # ZFS pool imports, and mount units. nixos-anywhere uses the same
      # config to format the disk during initial provisioning.
      inputs.disko.nixosModules.disko

      # ── Secrets management ───────────────────────────────────────────────
      #
      # agenix's NixOS module reads `age.secrets.*` options set in
      # nix/host/secrets.nix and decrypts .age files at boot using the
      # host's SSH key. Plaintext lands in /run/agenix/ (tmpfs, never persisted).
      inputs.agenix.nixosModules.default

      # ── Host configuration ───────────────────────────────────────────────
      #
      # nix/host/default.nix imports all sub-modules:
      #   hardware.nix     — Hyper-V guest, UEFI boot, ZFS kernel support
      #   networking.nix   — NAT for containers, SSH, firewall
      #   storage.nix      — disko ZFS layout (consumed by disko module above)
      #   secrets.nix      — agenix secret declarations
      #   agent-service.nix — systemd service for the orchestrator agent
      ../nix/host
    ];
  };
}
