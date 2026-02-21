# Flake checks — registered as `nix flake check` validations.
#
# These run in CI (nix-check job) and locally via `just check`.
{ ... }:
{
  perSystem =
    { pkgs, ... }:
    let
      mkContainer = import ../nix/mkContainer.nix;
      moduleLibrary = import ../nix/moduleLibrary.nix;

      # Smoke test: evaluate mkContainer with a minimal spec
      testSpec = {
        name = "check-basic";
        modules = [
          "git"
          "fish"
        ];
        owner = "test-user";
      };
      testResult = mkContainer testSpec;

      # Smoke test: evaluate with all available modules (derived from moduleLibrary)
      fullSpec = {
        name = "check-full";
        modules = builtins.attrNames moduleLibrary;
        owner = "test-user";
      };
      fullResult = mkContainer fullSpec;
    in
    {
      checks = {
        # Verify mkContainer evaluates successfully with a basic spec
        mkContainer-basic = pkgs.runCommand "mkContainer-basic-check" { } ''
          echo "mkContainer basic evaluation succeeded"
          echo "Container: ${builtins.head (builtins.attrNames testResult.containers)}"
          touch $out
        '';

        # Verify mkContainer evaluates with all modules
        mkContainer-full = pkgs.runCommand "mkContainer-full-check" { } ''
          echo "mkContainer full evaluation succeeded"
          echo "Container: ${builtins.head (builtins.attrNames fullResult.containers)}"
          touch $out
        '';

        # Verify container config has expected attributes
        mkContainer-structure = pkgs.runCommand "mkContainer-structure-check" { } ''
          echo "Checking container structure..."
          ${
            let
              container = testResult.containers.check-basic;
            in
            ''
              echo "privateNetwork: ${builtins.toJSON container.privateNetwork}"
              echo "autoStart: ${builtins.toJSON container.autoStart}"
            ''
          }
          echo "Structure check passed"
          touch $out
        '';
      };
    };
}
