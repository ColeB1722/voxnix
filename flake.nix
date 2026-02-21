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
        ./parts/formatting.nix
        ./parts/devshell.nix
        ./parts/modules.nix
        ./parts/host.nix
        ./parts/agent.nix
        ./parts/checks.nix
      ];
    };
}
