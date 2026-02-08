{
  description = "voxnix — agentic NixOS container and VM orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    treefmt-nix.url = "github:numtide/treefmt-nix";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      imports = [
        inputs.treefmt-nix.flakeModule
        # ./parts/host.nix
        # ./parts/modules.nix
        # ./parts/agent.nix
        # ./parts/checks.nix
      ];

      perSystem =
        { pkgs, ... }:
        {
          # Dev shell with all tools needed for development
          devShells.default = pkgs.mkShell {
            packages = with pkgs; [
              # Nix tools
              nixfmt
              nil # Nix LSP

              # Python
              python312
              uv
              ruff

              # General
              just
              jq
            ];

            shellHook = ''
              echo "voxnix dev shell"
            '';
          };

          # Formatting configuration
          treefmt = {
            projectRootFile = "flake.nix";
            programs.nixfmt.enable = true;
            programs.ruff-format.enable = true;
          };
        };
    };
}
