# Declarative disk layout for the voxnix appliance via disko.
#
# Designed for nixos-anywhere initial provisioning on a Hyper-V Gen 2 VM
# with a single VHDX virtual disk (~60 GB).
#
# Layout:
#   /dev/sda (GPT)
#   ├── p1: EFI System Partition (512 MB, FAT32) → /boot
#   └── p2: ZFS partition (remaining space)       → tank pool
#
# ZFS datasets:
#   tank/root     → /           (OS root — reproducible from Nix, safe to wipe)
#   tank/nix      → /nix        (store + profiles — largest dataset, no atime)
#   tank/var      → /var        (container state, logs, systemd journals)
#   tank/home     → /home       (admin home directory)
#   tank/users    → /tank/users (per-user container workspaces, dynamically extended)
#   tank/images   → /tank/images (precompiled image cache — future)
#
# The agent creates child datasets under tank/users at runtime:
#   tank/users/<chat_id>/containers/<name>/workspace
#
# See docs/architecture.md § Host Storage — ZFS for the full layout rationale.
{ config, lib, ... }:
{
  disko.devices = {
    disk.main = {
      type = "disk";
      # Hyper-V Gen 2 presents the VHDX via the storvsc SCSI controller as /dev/sda.
      # If your setup differs (e.g. NVMe passthrough), update this path.
      device = "/dev/sda";
      content = {
        type = "gpt";
        partitions = {
          ESP = {
            size = "512M";
            type = "EF00";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = [
                "umask=0077"
              ];
            };
          };
          zfs = {
            size = "100%";
            content = {
              type = "zfs";
              pool = "tank";
            };
          };
        };
      };
    };

    # ── ZFS pool ───────────────────────────────────────────────────────────

    zpool.tank = {
      type = "zpool";

      # Pool-level options (set once at pool creation).
      options = {
        # Optimal sector alignment for modern disks / VHDX (4K sectors).
        ashift = "12";
      };

      # Default dataset properties — inherited by all datasets unless overridden.
      rootFsOptions = {
        # zstd compression — excellent ratio for code repos, build artifacts, and logs.
        compression = "zstd";
        # POSIX ACLs — required by systemd and NixOS container infrastructure.
        acltype = "posixacl";
        # Store extended attributes in the inode — avoids separate SA lookup overhead.
        xattr = "sa";
        # Don't mount the pool root itself — only child datasets are mounted.
        mountpoint = "none";
        # Disable automatic snapshots at the pool level (opt-in per dataset).
        "com.sun:auto-snapshot" = "false";
      };

      datasets = {
        # ── OS datasets ────────────────────────────────────────────────────

        # Root filesystem — the OS root. Fully reproducible from the Nix
        # configuration, so no critical state lives here.
        "root" = {
          type = "zfs_fs";
          mountpoint = "/";
          options.mountpoint = "legacy";
        };

        # Nix store — by far the largest dataset. Holds /nix/store (all
        # packages, closures, build results) and /nix/var (profiles, GC roots).
        # atime=off: the store is content-addressed; access times are meaningless
        # and disabling them reduces write amplification significantly.
        "nix" = {
          type = "zfs_fs";
          mountpoint = "/nix";
          options = {
            mountpoint = "legacy";
            atime = "off";
          };
        };

        # Variable data — container rootfs (/var/lib/nixos-containers/),
        # systemd journals, logs, and service state directories.
        "var" = {
          type = "zfs_fs";
          mountpoint = "/var";
          options.mountpoint = "legacy";
        };

        # Admin home directory.
        "home" = {
          type = "zfs_fs";
          mountpoint = "/home";
          options.mountpoint = "legacy";
        };

        # ── Workload datasets ──────────────────────────────────────────────

        # Per-user workspace root. The agent creates child datasets at runtime:
        #   zfs create tank/users/<chat_id>
        #   zfs create tank/users/<chat_id>/containers/<name>/workspace
        #
        # These are bind-mounted into containers via mkContainer.
        # Quotas, snapshots, and atomic destroy all operate at dataset level.
        "users" = {
          type = "zfs_fs";
          mountpoint = "/tank/users";
          options.mountpoint = "legacy";
        };

        # Precompiled image cache — shared across all users (future).
        # Stores pre-built container/VM closures for fast provisioning.
        "images" = {
          type = "zfs_fs";
          mountpoint = "/tank/images";
          options.mountpoint = "legacy";
        };
      };
    };
  };
}
