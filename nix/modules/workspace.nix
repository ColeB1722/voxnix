# Workspace module — standard directory structure for dev containers.
#
# The /workspace path is the bind-mount target for per-user ZFS datasets
# from the host (tank/users/<owner>/workspace → /workspace).
# The host's mkContainer wiring handles the actual bind mount;
# this module just ensures the mount point and directory structure exist.
{ lib, config, ... }:
{
  # Create the workspace mount point and common subdirectories
  systemd.tmpfiles.rules = [
    "d /workspace 0755 root root -"
    "d /workspace/projects 0755 root root -"
  ];

  # Set workspace as the default working directory for login shells
  environment.extraInit = ''
    if [ -d /workspace ]; then
      cd /workspace
    fi
  '';
}
