# Python agent packaging — bundles the repo source for the agent systemd service.
#
# The agent is a Python application with dependencies managed by uv (not nixpkgs).
# Rather than packaging each Python dependency as a Nix derivation, we bundle the
# full repo source into /nix/store and let uv handle the virtualenv at runtime
# (see nix/host/agent-service.nix for the systemd service that runs uv sync).
#
# This module exports:
#   - packages.voxnix-src — the full repo source as a store path
#
# The host config references this package via specialArgs to set VOXNIX_FLAKE_PATH
# and the systemd service's WorkingDirectory.
#
# Why the full repo (not just agent/)?
#   The agent needs access to nix/mkContainer.nix, nix/moduleLibrary.nix, and
#   nix/modules/ at runtime — the Nix expression generator references these paths
#   via VOXNIX_FLAKE_PATH. Bundling the full repo keeps everything in one
#   atomic store path that updates together on each nixos-rebuild.
{ lib, ... }:
{
  perSystem =
    { pkgs, ... }:
    {
      packages.voxnix-src = pkgs.stdenvNoCC.mkDerivation {
        pname = "voxnix-src";
        version = "0.1.0";

        # lib.cleanSource strips .git and other VCS metadata, ensuring the
        # derivation hash doesn't change on irrelevant .git state updates.
        # Additional filtering removes dev-time artifacts that don't belong
        # in the production store path.
        src = lib.cleanSourceWith {
          src = ../.;
          filter =
            path: type:
            let
              baseName = builtins.baseNameOf path;
            in
            !(builtins.elem baseName [
              ".direnv"
              ".venv"
              ".pytest_cache"
              ".ruff_cache"
              ".mypy_cache"
              "__pycache__"
              "result"
              ".github"
              "secrets"
            ]);
        };

        # No build phase — this is a pure source copy.
        dontBuild = true;
        dontConfigure = true;
        dontFixup = true;

        installPhase = ''
          runHook preInstall
          mkdir -p $out
          cp -r . $out/
          runHook postInstall
        '';

        meta = {
          description = "Voxnix agent and Nix module source tree";
          license = lib.licenses.mit;
        };
      };
    };
}
