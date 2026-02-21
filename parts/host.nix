# NixOS appliance host configuration.
#
# Will define: nixosConfigurations.appliance
# This is the full NixOS host config consumed by:
#   - nixos-anywhere (initial provisioning)
#   - nixos-rebuild --target-host (ongoing updates)
#   - nix build .#appliance (CI validation)
#
# Deferred until a target host is available for deployment.
{ ... }:
{
  # TODO: Define nixosConfigurations.appliance with:
  #   - extra-container + nixos-container
  #   - ZFS storage (via disko)
  #   - agenix secrets
  #   - SSH admin access (LAN only)
  #   - Orchestrator agent systemd service
}
