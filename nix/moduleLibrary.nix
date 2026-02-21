# Module library — single source of truth for available workload modules.
#
# Auto-discovers .nix files in nix/modules/. To add a new module,
# just drop a .nix file in that directory — it's automatically:
#   1. Available in mkContainer's module library
#   2. Listed in lib.availableModules
#   3. Exported in nixosModules
#
# Convention: filenames should be lowercase, hyphen-separated, e.g. code-server.nix.
# The module name is the filename without .nix (e.g. "code-server").
#
# Returns: { git = /path/to/modules/git.nix; fish = /path/to/modules/fish.nix; ... }

let
  modulesDir = ./modules;
  dirContents = builtins.readDir modulesDir;

  # Filter to only .nix files (excludes directories, .gitkeep, READMEs, etc.)
  isNixModule = name: type: type == "regular" && builtins.match ".+\\.nix" name != null;

  nixFileNames = builtins.filter (name: isNixModule name dirContents.${name}) (
    builtins.attrNames dirContents
  );

  # Strip .nix suffix to get the module name
  toModuleName = filename: builtins.replaceStrings [ ".nix" ] [ "" ] filename;
in
builtins.listToAttrs (
  map (filename: {
    name = toModuleName filename;
    value = modulesDir + "/${filename}";
  }) nixFileNames
)
