# Networking configuration for the voxnix appliance.
#
# Provides:
#   - Outbound bridge (br-vox) for container internet access
#   - NAT/masquerading from bridge to host's external interface
#   - dnsmasq DHCP server on the bridge (10.100.0.0/24)
#   - SSH server (key-based, LAN only)
#   - Firewall with minimal open ports
#
# All containers use privateNetwork=true + hostBridge="br-vox".
# The bridge provides internet connectivity so Tailscale can enroll
# and packages can be downloaded inside containers.
#
# Network layout:
#   Host bridge:    10.100.0.1/24  (br-vox)
#   DHCP range:     10.100.0.100–200
#   NAT:            br-vox → eth0
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

  # Use DHCP for the host's external interface.
  # Hyper-V synthetic NIC appears as eth0 (hv_netvsc driver).
  networking.useDHCP = true;

  # ── Outbound bridge for containers ─────────────────────────────────────────

  # br-vox: the shared outbound bridge all voxnix containers connect to.
  # Each container gets a virtual ethernet attached to this bridge via nspawn's
  # --network-bridge flag (set by hostBridge = "br-vox" in mkContainer.nix).
  # The bridge acts as the default gateway; the host NATs traffic to eth0.
  #
  # The bridge has no physical interfaces — it is purely virtual, acting as
  # a software switch between containers and the host NAT.
  networking.bridges.br-vox.interfaces = [ ];

  networking.interfaces."br-vox".ipv4.addresses = [
    {
      address = "10.100.0.1";
      prefixLength = 24;
    }
  ];

  # ── DHCP for the bridge ────────────────────────────────────────────────────

  # dnsmasq serves DHCP leases on br-vox so containers get IPs automatically
  # without requiring static IP allocation in mkContainer.nix.
  # DNS queries from containers are forwarded to the host's upstream resolvers.
  services.dnsmasq = {
    enable = true;
    settings = {
      # Only listen on the bridge — do not interfere with eth0 or other interfaces.
      interface = "br-vox";
      bind-interfaces = true;

      # DHCP range: 10.100.0.100–200, 12-hour lease.
      dhcp-range = [ "10.100.0.100,10.100.0.200,12h" ];

      # Use the host's upstream resolvers for DNS forwarding.
      # Containers inherit the host's DNS configuration.
      no-resolv = false;
    };
  };

  # ── NAT for containers ─────────────────────────────────────────────────────

  # Masquerade container traffic from br-vox out through eth0.
  # This gives every container internet access so Tailscale can enroll
  # and outbound connections (package downloads, etc.) work.
  networking.nat = {
    enable = true;

    # br-vox: shared outbound bridge used by all containers via hostBridge.
    # ve-+:   point-to-point veth pairs, if any container uses localAddress
    #         instead of hostBridge (kept for forward compatibility).
    internalInterfaces = [
      "br-vox"
      "ve-+"
    ];

    # Hyper-V synthetic NIC. Update if your interface name differs.
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

    # Trust the container bridge and any point-to-point veth interfaces.
    # Containers need unrestricted outbound access through the host
    # (DNS, gateway, Tailscale enrollment, package downloads).
    # Inter-container isolation is enforced by separate network namespaces,
    # not by the host firewall.
    trustedInterfaces = [
      "br-vox"
      "ve-+"
    ];
  };
}
