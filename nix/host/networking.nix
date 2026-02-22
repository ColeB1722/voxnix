# Networking configuration for the voxnix appliance.
#
# Provides:
#   - NAT for containers with privateNetwork=true to reach the internet
#   - SSH server (key-based, LAN only)
#   - Firewall with minimal open ports
#   - Container veth interfaces trusted through the firewall
#
# All containers use privateNetwork=true (see docs/architecture.md § Networking Model).
# The host performs NAT so containers can connect outbound (Tailscale, package
# downloads, etc.) without exposing the host's network namespace.
#
# See docs/architecture.md § Host networking and § Inter-workload networking.
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # ── Host network ───────────────────────────────────────────────────────────

  networking.hostName = "voxnix";

  # Use networkd for predictable interface management.
  # Hyper-V synthetic NIC appears as eth0 (hv_netvsc driver).
  networking.useDHCP = true;

  # ── NAT for containers ─────────────────────────────────────────────────────

  # nixos-container with privateNetwork=true creates a veth pair per container:
  #   host side:  ve-<name>    (e.g. ve-dev-abc)
  #   container:  eth0         (inside the container's network namespace)
  #
  # Containers get IPs in the 10.233.x.0/24 range (NixOS default).
  # The host acts as the gateway and NATs outbound traffic.
  networking.nat = {
    enable = true;

    # Wildcard match — covers all current and future container veth interfaces.
    internalInterfaces = [ "ve-+" ];

    # Hyper-V synthetic NIC. If your interface name differs, update this.
    # Check with `ip link` after first boot.
    externalInterface = "eth0";
  };

  # ── SSH ────────────────────────────────────────────────────────────────────

  # Key-based SSH for admin maintenance and nixos-rebuild --target-host.
  # LAN only by design — not exposed externally.
  # See docs/architecture.md § SSH Admin Access.
  services.openssh = {
    enable = true;

    settings = {
      # No root login — use admin + sudo instead.
      PermitRootLogin = "no";

      # Key-based auth only — no passwords.
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;

      # Harden defaults.
      X11Forwarding = false;
      MaxAuthTries = 3;
    };
  };

  # ── Firewall ───────────────────────────────────────────────────────────────

  networking.firewall = {
    enable = true;

    # SSH is the only service exposed on the host's external interface.
    allowedTCPPorts = [ 22 ];

    # Trust all container veth interfaces — containers need unrestricted
    # communication with the host (DNS, gateway, package downloads).
    # Inter-container isolation is enforced by separate network namespaces,
    # not by the host firewall.
    trustedInterfaces = [ "ve-+" ];
  };
}
