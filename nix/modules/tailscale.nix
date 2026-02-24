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
# Why Type=simple (not oneshot):
#   NixOS implicitly adds Before=multi-user.target to any unit with
#   wantedBy=["multi-user.target"]. With Type=oneshot, systemd-nspawn cannot
#   send its sd_notify READY=1 signal to the host until the oneshot script
#   exits — i.e., until `tailscale up` completes. `tailscale up` contacts the
#   Tailscale control plane and can take 30-120 seconds on first enrollment.
#   The host container@.service has TimeoutStartSec=1min, so the container is
#   killed and restarted in a loop before enrollment ever completes.
#
#   With Type=simple, systemd considers the service "started" as soon as the
#   process launches (not when it exits). multi-user.target can then proceed,
#   nspawn sends READY to the host, and `tailscale up` runs to completion in
#   the background without racing against the 1-minute host timeout.
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

  # Background service that runs `tailscale up` with the injected auth key.
  # Uses Type=simple so multi-user.target is not blocked waiting for
  # enrollment to complete (see Why Type=simple above).
  #
  # On subsequent boots where the node is already registered, tailscaled
  # reconnects automatically — this service exits early via the status check.
  systemd.services.tailscale-autoconnect = {
    description = "Automatic Tailscale enrollment for voxnix container";
    after = [
      "network.target"
      "tailscaled.service"
    ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];

    serviceConfig = {
      # simple: service is considered started as soon as the process launches.
      # This prevents the unit from blocking multi-user.target (and therefore
      # the nspawn READY notification to the host) while tailscale up runs.
      Type = "simple";
      # Retry on transient failures (control plane unreachable, DNS hiccup,
      # auth key rate-limited). Without this, a single failed `tailscale up`
      # leaves the container permanently disconnected until a manual restart.
      Restart = "on-failure";
      RestartSec = 15;
    };

    script = ''
      # Source NixOS environment — TAILSCALE_AUTH_KEY is injected here via
      # environment.variables in mkContainer.nix. systemd services do not
      # source /etc/set-environment automatically (that's only for login shells),
      # so we must do it explicitly.
      if [ -f /etc/set-environment ]; then
        source /etc/set-environment
      fi

      # Wait for tailscaled socket to be ready (poll up to 30s).
      for i in $(seq 1 30); do
        if ${pkgs.tailscale}/bin/tailscale status >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done

      # Check if already connected — exit early if so (idempotent on restart).
      # tailscaled reconnects automatically on subsequent boots; we only need
      # to run `tailscale up` on first enrollment.
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
