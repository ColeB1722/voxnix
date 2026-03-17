# Dev shell — provides all tools needed for voxnix development.
#
# Standalone devenv config (coexists with flake.nix, which handles NixOS infra).
# Activated automatically via direnv when entering the repo.
#
# https://devenv.sh/reference/options/
{ pkgs, ... }:
{
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
    treefmt # reads treefmt.toml for formatter config
    secretspec # declarative local secrets (eval + bot profiles via 1Password)

    # Deployment
    nixos-anywhere # initial provisioning: just provision <ip>
    ragenix # agenix CLI for editing/rekeying secrets (ragenix = agenix in nixpkgs; agenix itself is flake-only)
  ];

  # Pre-commit hooks.
  # treefmt covers Nix (nixfmt) and Python formatting (ruff-format) via treefmt.toml.
  # The ruff lint + ty type-check hooks run only when agent/ files are staged.
  git-hooks.hooks = {
    treefmt = {
      enable = true;
    };

    # Ruff lint scoped to agent/ — runs only when Python files are staged
    ruff-agent = {
      enable = true;
      name = "ruff-agent";
      description = "Lint agent/ Python code with ruff";
      entry = "${pkgs.ruff}/bin/ruff check agent/";
      language = "system";
      types = [ "python" ];
      pass_filenames = false;
      files = "^agent/";
    };

    # Type-check agent/ with ty — runs only when agent/ files are staged
    ty-agent = {
      enable = true;
      name = "ty-agent";
      description = "Type-check agent/ with ty";
      entry = "${pkgs.uv}/bin/uv run ty check agent/";
      language = "system";
      types = [ "python" ];
      pass_filenames = false;
      files = "^agent/";
    };
  };

  enterShell = ''
    echo "voxnix dev shell"
  '';
}
