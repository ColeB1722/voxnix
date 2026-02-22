# Hardware configuration for the voxnix appliance on Hyper-V.
#
# Target: Windows 11 Pro Hyper-V Generation 2 VM
# - UEFI boot via systemd-boot
# - Hyper-V guest integration services (graceful shutdown, KVP, etc.)
# - ZFS kernel support + ARC memory cap
# - Kernel tuning for container-heavy workloads
#
# See docs/architecture.md § NixOS Host and README.md § Hyper-V.
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # ── Boot ───────────────────────────────────────────────────────────────────

  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Limit boot entries to prevent /boot from filling up on a small ESP.
  boot.loader.systemd-boot.configurationLimit = 10;

  # ── Hyper-V guest support ──────────────────────────────────────────────────

  # Enables Hyper-V integration services:
  #   - hv_utils:    graceful shutdown via Stop-VM (ACPI), KVP exchange
  #   - hv_storvsc:  SCSI storage controller (VHDX access)
  #   - hv_netvsc:   synthetic network adapter
  #   - hv_vmbus:    VMBus transport for all integration components
  #
  # Guest services and Shutdown must be enabled in Hyper-V VM settings
  # for graceful shutdown to work (important for clean ZFS export).
  virtualisation.hypervGuest.enable = true;

  # Video driver for Hyper-V console (basic framebuffer — headless, but
  # useful for emergency console access via Hyper-V Manager).
  boot.initrd.kernelModules = [ "hv_storvsc" ];

  # ── ZFS ────────────────────────────────────────────────────────────────────

  boot.supportedFilesystems = [ "zfs" ];

  # Required for ZFS — must be unique per machine. 8 hex characters.
  # Generated once; safe to keep across rebuilds. Change only if cloning
  # the VM to a second machine.
  networking.hostId = "a1c3e5f7";

  # Cap ZFS ARC at 8 GB to leave headroom for containers and the agent.
  # Host has 16–32 GB total; containers and the agent need the rest.
  # See docs/architecture.md § Host Storage — ZFS.
  boot.kernelParams = [
    "zfs.zfs_arc_max=${toString (8 * 1024 * 1024 * 1024)}"
  ];

  # ZFS automatic scrub — catches silent data corruption early.
  services.zfs.autoScrub = {
    enable = true;
    interval = "monthly";
  };

  # ZFS automatic TRIM — reclaims unused blocks on the VHDX (thin provisioning).
  services.zfs.trim.enable = true;

  # ── Kernel tuning ──────────────────────────────────────────────────────────

  # IP forwarding — required for container NAT (see networking.nix).
  boot.kernel.sysctl = {
    "net.ipv4.ip_forward" = 1;
  };
}
