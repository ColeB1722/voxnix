# mkContainer — core Nix function for voxnix.
#
# Consumes a spec (attrset, typically deserialized from JSON) and composes
# workload modules into a NixOS container definition for extra-container.
#
# Spec format:
# {
#   name    : string       — container name (e.g. "dev-abc")
#   modules : [string]     — module names from the module library (e.g. ["git" "fish"])
#   owner   : string       — owner identifier, typically a Telegram chat_id
# }
#
# Returns:
# {
#   containers.<name> = { ... };   — NixOS container configuration
# }
#
# Usage (from Nix):
#   mkContainer { name = "dev-abc"; modules = ["git" "fish"]; owner = "chat_id_123"; }
#
# Usage (from CLI with JSON):
#   nix eval .#lib.mkContainer --apply 'f: f (builtins.fromJSON (builtins.readFile ./spec.json))'

let
  # Single source of truth — auto-discovered from nix/modules/.
  moduleLibrary = import ./moduleLibrary.nix;

  # Resolve a module name to its path, with a clear error on unknown modules.
  resolveModule =
    name:
    if builtins.hasAttr name moduleLibrary then
      moduleLibrary.${name}
    else
      throw "voxnix: unknown module '${name}'. Available: ${builtins.concatStringsSep ", " (builtins.attrNames moduleLibrary)}";

  # Validate that a spec has all required fields.
  validateSpec =
    spec:
    let
      required = [
        "name"
        "modules"
        "owner"
      ];
      missing = builtins.filter (f: !(builtins.hasAttr f spec)) required;
    in
    if missing != [ ] then
      throw "voxnix: mkContainer spec missing required fields: ${builtins.concatStringsSep ", " missing}"
    else if !builtins.isList spec.modules then
      throw "voxnix: mkContainer spec.modules must be a list"
    else if !builtins.isString spec.name || spec.name == "" then
      throw "voxnix: mkContainer spec.name must be a non-empty string"
    else if !builtins.isString spec.owner || spec.owner == "" then
      throw "voxnix: mkContainer spec.owner must be a non-empty string"
    else
      spec;

in
spec:
let
  validSpec = validateSpec spec;

  # Force strict evaluation of module resolution so unknown module errors
  # surface immediately at spec evaluation time, not lazily at build time.
  resolvedModules =
    let
      resolved = map resolveModule validSpec.modules;
    in
    builtins.deepSeq resolved resolved;

  # Base configuration applied to every container — shared defaults
  # that all voxnix containers get regardless of selected modules.
  baseConfig =
    { ... }:
    {
      system.stateVersion = "25.05";

      # Minimal firewall — allow nothing inbound by default.
      # Individual modules can open ports as needed.
      networking.firewall.enable = true;

      # Tag the container with owner metadata via environment variable.
      # The agent uses this for ownership verification.
      environment.variables.VOXNIX_OWNER = validSpec.owner;
      environment.variables.VOXNIX_CONTAINER = validSpec.name;
    };
in
# Force strict evaluation of resolvedModules before returning the result.
# Without this, Nix's laziness means unknown module errors only surface
# when the container is actually built — deepSeq ensures they fire immediately.
builtins.deepSeq resolvedModules {
  containers.${validSpec.name} = {
    # All containers use private networking — see architecture.md § Networking Model.
    # Inter-container communication goes through the shared bridge.
    privateNetwork = true;
    autoStart = true;

    config =
      { ... }:
      {
        imports = [ baseConfig ] ++ resolvedModules;
      };
  };
}
