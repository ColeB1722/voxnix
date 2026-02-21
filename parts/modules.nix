# Workload module library and mkContainer — wired into the flake.
#
# Everything derives from nix/moduleLibrary.nix (auto-discovered).
# To add a module, just drop a .nix file in nix/modules/ — no edits needed here.
#
# Exports:
#   flake.lib.mkContainer      — function: spec → container config
#   flake.lib.availableModules  — list of module names the agent can offer
#   flake.nixosModules.*        — individual NixOS modules for direct import
{ ... }:
let
  moduleLibrary = import ../nix/moduleLibrary.nix;
in
{
  flake = {
    # The core container builder — consumes a JSON-derived spec,
    # composes workload modules, produces a container definition.
    lib.mkContainer = import ../nix/mkContainer.nix;

    # Module names available for use in specs. The agent queries this
    # to discover what it can offer users (no hardcoded list in Python).
    # Derived from moduleLibrary — adding a .nix file to nix/modules/
    # automatically makes it available here.
    lib.availableModules = builtins.attrNames moduleLibrary;

    # Individual NixOS modules exported for direct consumption.
    # Useful for testing modules in isolation or importing them
    # outside of the mkContainer pipeline.
    nixosModules = moduleLibrary;
  };
}
