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

              # Install pre-commit hook if not already present
              if [ ! -f .git/hooks/pre-commit ] || ! grep -q "voxnix" .git/hooks/pre-commit; then
                mkdir -p .git/hooks
                cat > .git/hooks/pre-commit << 'HOOK'
              #!/usr/bin/env bash
              # voxnix pre-commit hook — installed by dev shell
              set -euo pipefail

              echo "Running pre-commit checks..."

              # Format check (treefmt)
              if ! treefmt --check 2>/dev/null; then
                echo "❌ Formatting issues found. Run 'treefmt' to fix."
                exit 1
              fi

              # Lint Python if agent/ files are staged
              if git diff --cached --name-only | grep -q '^agent/'; then
                if ! ruff check agent/; then
                  echo "❌ Ruff found issues in agent/."
                  exit 1
                fi
              fi

              echo "✅ Pre-commit checks passed."
              HOOK
                chmod +x .git/hooks/pre-commit
              fi
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
