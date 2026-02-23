# Tailscale module — per-container tailnet enrollment for voxnix containers.
#
# Each container that includes this module runs its own tailscaled and gets
# its own identity on the user's tailnet. Users connect directly to their
# containers via Tailscale SSH — no NAT, port forwarding, or reverse proxy
# on the host.
#
# Auth key injection:
#   The TAILSCALE_AUTH_KEY environment variable is set by mkContainer.nix
#   (from spec.tailscaleAuthKey, populated by the agent from VoxnixSettings).
#   The tailscale-autoconnect oneshot service reads it on first boot.
#
# Hostname:
#   Set to VOXNIX_CONTAINER (the container name), also injected by mkContainer.nix.
#   This gives each container a predictable name on the tailnet
#   (e.g. "dev-abc" → ssh root@dev-abc via Tailscale).
#
# Tailscale SSH:
#   The --ssh flag enables Tailscale SSH, so users can `ssh root@<container-name>`
#   via their tailnet without managing SSH keys inside the container.
#
# See docs/architecture.md § Private access — Tailscale.
{ pkgs, ... }:
{
  # Enable the Tailscale daemon.
  services.tailscale.enable = true;

  # Allow Tailscale's WireGuard traffic through the container firewall.
  networking.firewall.allowedUDPPorts = [ 41641 ];

  # Allow incoming SSH via Tailscale (port 22 is used by Tailscale SSH proxy).
  networking.firewall.allowedTCPPorts = [ 22 ];

  # Oneshot service that runs `tailscale up` with the injected auth key
  # on first boot. Subsequent boots where the node is already registered
  # are handled gracefully by tailscale (it reconnects automatically).
  systemd.services.tailscale-autoconnect = {
    description = "Automatic Tailscale enrollment for voxnix container";
    after = [
      "network-pre.target"
      "tailscaled.service"
    ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };

    script = ''
      # Wait for tailscaled socket to be ready.
      sleep 2

      # Check if already connected — skip if so (idempotent on restart).
      status=$(${pkgs.tailscale}/bin/tailscale status --json 2>/dev/null | ${pkgs.jq}/bin/jq -r '.BackendState // "NoState"')
      if [ "$status" = "Running" ]; then
        echo "Tailscale already connected, skipping enrollment."
        exit 0
      fi

      # Require auth key — fail clearly if not injected.
      if [ -z "$TAILSCALE_AUTH_KEY" ]; then
        echo "ERROR: TAILSCALE_AUTH_KEY not set. Cannot enroll in tailnet."
        exit 1
      fi

      # Use VOXNIX_CONTAINER as the tailnet hostname (set by mkContainer.nix).
      # Falls back to system hostname if not set (shouldn't happen in practice).
      hostname="''${VOXNIX_CONTAINER:-$(hostname)}"

      ${pkgs.tailscale}/bin/tailscale up \
        --auth-key="$TAILSCALE_AUTH_KEY" \
        --hostname="$hostname" \
        --accept-routes=false \
        --ssh
    '';
  };
}
