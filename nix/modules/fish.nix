# Fish shell module for voxnix containers
#
# Enables fish as the default interactive shell.
{ pkgs, ... }:
{
  programs.fish.enable = true;
  users.defaultUserShell = pkgs.fish;
}
