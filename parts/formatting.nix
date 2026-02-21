# Formatting configuration — treefmt with nixfmt + ruff.
#
# Extracted from flake.nix for modularity. Imported as a flake-parts module.
{ inputs, ... }:
{
  imports = [ inputs.treefmt-nix.flakeModule ];

  perSystem = {
    treefmt = {
      projectRootFile = "flake.nix";
      programs.nixfmt.enable = true;
      programs.ruff-format.enable = true;
    };
  };
}
