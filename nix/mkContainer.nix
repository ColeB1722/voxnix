# mkContainer — core Nix function for voxnix.
#
# Consumes a spec (attrset, typically deserialized from JSON) and composes
# workload modules into a NixOS container definition for extra-container.
#
# Spec format:
# {
#   name      : string            — container name (e.g. "dev-abc")
#   modules   : [string]          — module names from the module library (e.g. ["git" "fish"])
#   owner     : string            — owner identifier, typically a Telegram chat_id
#   workspace : string (optional) — host path to bind-mount at /workspace in the container
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

  # Optional Tailscale auth key — when spec.tailscaleAuthKey is set, inject it
  # as an environment variable so the tailscale module's autoconnect service
  # can read it. This follows the same pattern as VOXNIX_OWNER injection.
  hasTailscaleKey =
    builtins.hasAttr "tailscaleAuthKey" validSpec
    && builtins.isString (validSpec.tailscaleAuthKey or "")
    && (validSpec.tailscaleAuthKey or "") != "";
  tailscaleConfig =
    { ... }:
    {
      environment.variables.TAILSCALE_AUTH_KEY = validSpec.tailscaleAuthKey;
    };

  # Optional workspace bind mount — when spec.workspace is set (a host path string),
  # the ZFS dataset at that path is bind-mounted into the container at /workspace.
  # The workspace module (nix/modules/workspace.nix) ensures the mount point exists
  # inside the container via systemd.tmpfiles.rules.
  #
  # When spec.workspace is absent, no bind mount is added — the workspace module
  # still creates /workspace as an ephemeral directory inside the container.
  hasWorkspace =
    builtins.hasAttr "workspace" validSpec && builtins.isString (validSpec.workspace or "");
  workspaceBindMounts =
    if hasWorkspace then
      {
        "/workspace" = {
          hostPath = validSpec.workspace;
          isReadWrite = true;
        };
      }
    else
      { };
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

    # Workspace bind mount (empty attrset when no workspace configured).
    bindMounts = workspaceBindMounts;

    # Allow /dev/net/tun inside the container — required by Tailscale for
    # kernel-mode WireGuard. systemd-nspawn blocks device access by default;
    # this allowedDevices entry grants read-write access to the TUN device.
    allowedDevices = [
      {
        node = "/dev/net/tun";
        modifier = "rwm";
      }
    ];

    config =
      { ... }:
      {
        imports = [
          baseConfig
        ]
        ++ (if hasTailscaleKey then [ tailscaleConfig ] else [ ])
        ++ resolvedModules;
      };
  };
}
