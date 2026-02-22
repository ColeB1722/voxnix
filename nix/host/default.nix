# Top-level NixOS host configuration for the voxnix appliance.
#
# This module imports all sub-modules that compose the full appliance.
# Each sub-module is independently readable and testable.
#
# Consumed by parts/host.nix via nixosConfigurations.appliance.
{
  config,
  lib,
  pkgs,
  ...
}:
{
  imports = [
    ./hardware.nix
    ./networking.nix
    ./storage.nix
    ./secrets.nix
    ./agent-service.nix
  ];

  # ── Base system ────────────────────────────────────────────────────────────

  system.stateVersion = "25.05";
  time.timeZone = "UTC";

  # Headless appliance — no desktop, no GUI.
  documentation.enable = false;

  # ── Nix configuration ─────────────────────────────────────────────────────

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    # Keep build dependencies around for faster rebuilds on the appliance.
    keep-outputs = true;
    keep-derivations = true;
  };

  # Automatic garbage collection — prevent /nix/store from growing unbounded.
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };

  # ── Admin user ─────────────────────────────────────────────────────────────

  # The admin user is the only human account on the appliance.
  # SSH key-based access only — no password, no root login.
  # Replace the placeholder key before deploying.
  users.users.admin = {
    isNormalUser = true;
    extraGroups = [
      "wheel"
      "systemd-journal"
    ];
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMYEMoAMxPAGD4AzBPCAYV6UiHrAeMm/AJIGXKCikkuc"
    ];
    # No password — SSH key only.
    hashedPassword = "!";
  };

  # Allow admin to use sudo without a password (required for nixos-rebuild --use-remote-sudo).
  security.sudo.wheelNeedsPassword = false;

  # Disable root login entirely — admin + sudo is the only path.
  users.users.root.hashedPassword = "!";

  # ── Container runtime ──────────────────────────────────────────────────────

  # Enable the NixOS container infrastructure — this installs the
  # container@.service systemd template unit that extra-container and
  # nixos-container use to start/stop containers. Without this, container
  # builds succeed but `extra-container create --start` fails with
  # "Unit container@<name>.service not found".
  boot.enableContainers = true;

  # Required by extra-container for dynamic container installation.
  # extra-container installs per-container systemd units into a mutable
  # directory rather than rebuilding the host system. Without this path,
  # `extra-container create` fails with:
  # "Container service installation failed. Please add
  #  boot.extraSystemdUnitPaths = [ "/etc/systemd-mutable/system" ]"
  # See: https://github.com/erikarvstedt/extra-container/#install
  boot.extraSystemdUnitPaths = [ "/etc/systemd-mutable/system" ];

  # extra-container allows creating declarative NixOS containers without a
  # full system rebuild. The agent pipes generated Nix expressions to it.
  # See docs/architecture.md § extra-container — key discovery.
  environment.systemPackages = with pkgs; [
    extra-container
    git
    jq
  ];

  # ── Minimal utilities ──────────────────────────────────────────────────────

  programs.fish.enable = true;

  environment.variables = {
    EDITOR = "nano";
  };
}
