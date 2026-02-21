# Dev shell — provides all tools needed for voxnix development.
#
# Enter with: nix develop
{ ... }:
{
  perSystem =
    { config, pkgs, ... }:
    {
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
          config.treefmt.build.wrapper
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
          if ! treefmt --fail-on-change 2>/dev/null; then
            echo "❌ Formatting issues found. Run 'treefmt' to fix."
            exit 1
          fi

          # Lint and type-check Python if agent/ files are staged
          if git diff --cached --name-only | grep -q '^agent/'; then
            if ! ruff check agent/; then
              echo "❌ Ruff found issues in agent/."
              exit 1
            fi
            if ! uv sync --all-extras --quiet; then
              echo "❌ uv sync failed — run 'uv sync --all-extras' to fix."
              exit 1
            fi
            if ! uv run ty check agent/; then
              echo "❌ ty found type errors in agent/."
              exit 1
            fi
          fi

          echo "✅ Pre-commit checks passed."
          HOOK
            chmod +x .git/hooks/pre-commit
          fi
        '';
      };
    };
}
