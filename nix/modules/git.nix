# Git module — provides git with sensible defaults for dev containers.
#
# Included in containers via mkContainer when the spec lists "git".
{ pkgs, ... }:
{
  environment.systemPackages = [ pkgs.git ];

  # System-wide git defaults (can be overridden per-user)
  environment.etc."gitconfig".text = ''
    [init]
      defaultBranch = main
    [core]
      autocrlf = input
    [pull]
      rebase = true
  '';
}
