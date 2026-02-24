# Systemd service for the voxnix orchestrator agent.
#
# The agent runs directly on the host (not in a container) because it needs
# host-level access to machinectl, extra-container, nixos-container, and ZFS.
#
# Python dependency management:
#   The agent is a Python application with dependencies not available in nixpkgs
#   (pydantic-ai, logfire, etc.). Rather than packaging each dependency as a Nix
#   derivation, we use uv to manage the virtualenv at runtime:
#
#   1. The full repo source is bundled into /nix/store via the voxnix-src derivation
#   2. ExecStartPre runs `uv sync --frozen` to install deps into a persistent venv
#      at /var/lib/voxnix-agent/.venv (StateDirectory)
#   3. ExecStart runs the bot via `uv run python -m agent.chat`
#
#   This approach is pragmatic for MVP — uv handles the complex Python deps while
#   Nix handles everything else. The venv is cached across restarts; uv sync is
#   a no-op when deps haven't changed.
#
# Secrets:
#   All runtime credentials (Telegram token, LLM key, etc.) are injected via
#   agenix EnvironmentFile. The agent reads them through pydantic-settings
#   (agent/config.py). See secrets.nix and docs/architecture.md § Secrets Management.
#
# VOXNIX_FLAKE_PATH:
#   Points the agent at the repo source in /nix/store so the Nix expression
#   generator can find nix/mkContainer.nix. Set directly in the service
#   environment (not a secret — it's a store path).
#
# See docs/architecture.md § Orchestrator Agent and § Agent Tool Architecture.
{
  config,
  lib,
  pkgs,
  inputs,
  voxnix-src,
  ...
}:
let
  # Python interpreter — matches the version in pyproject.toml (requires-python >= 3.12).
  python = pkgs.python312;

  # uv — fast Python package manager. Handles venv creation and dep installation.
  uv = pkgs.uv;

  # Persistent venv location — survives service restarts and rebuilds.
  # Only recreated when Python version or deps change (uv handles this).
  venvDir = "/var/lib/voxnix-agent/.venv";

  # uv cache — avoids re-downloading packages on every sync.
  uvCacheDir = "/var/lib/voxnix-agent/uv-cache";
in
{
  # Pre-create directories listed in ReadWritePaths that may not exist on a
  # fresh system. ProtectSystem=strict requires all ReadWritePaths to exist
  # at service start time, or systemd can't set up the mount namespace.
  #
  # /var/lib/nixos-containers — created by nixos-container on first use, but
  # the agent service starts before any container has ever been created.
  # /tank — parent directory for ZFS user datasets; exists after ZFS import
  # but creating it here is a safe no-op if it already exists.
  systemd.tmpfiles.rules = [
    "d /var/lib/nixos-containers 0755 root root -"
    "d /tank 0755 root root -"
    "d /tank/users 0755 root root -"
    "d /tank/images 0755 root root -"
  ];

  systemd.services.voxnix-agent = {
    description = "Voxnix orchestrator agent (Telegram bot)";
    documentation = [ "https://github.com/ColeB1722/voxnix" ];

    # Give up after 5 consecutive failures within 5 minutes.
    # Prevents infinite restart loops on persistent config errors
    # (bad token, wrong API key, etc.).
    # These are [Unit] directives, not [Service] — must be outside serviceConfig.
    startLimitBurst = 5;
    startLimitIntervalSec = 300;

    # Start after networking is fully online (agent needs outbound HTTPS for
    # Telegram API and LLM providers) and after agenix has decrypted secrets.
    after = [
      "network-online.target"
      "agenix.service"
    ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];

    # ── Environment ──────────────────────────────────────────────────────────

    # Packages on PATH — the agent shells out to these via run_command().
    # Using `path` instead of `environment.PATH` avoids conflicts with the
    # default PATH that NixOS's systemd module sets for all services.
    path = [
      python
      uv
      pkgs.nix
      pkgs.extra-container
      pkgs.systemd # machinectl
      pkgs.zfs
      pkgs.coreutils
      pkgs.bash
      pkgs.nixos-container
      pkgs.sudo
    ];

    environment = {
      # Point the agent at the repo source so nix_gen/generator.py can find
      # nix/mkContainer.nix. This is a /nix/store path — immutable and
      # updated atomically on each nixos-rebuild.
      VOXNIX_FLAKE_PATH = "${voxnix-src}";

      # ZFS pool name — must match the pool defined in nix/host/storage.nix.
      # Python reads this via VoxnixSettings.zfs_pool (env var ZFS_POOL).
      # Defining it here keeps the Nix config as the single source of truth:
      # if the pool is ever renamed, only this file needs updating.
      ZFS_POOL = "tank";

      # uv environment configuration — use the persistent venv and cache
      # in the service's StateDirectory rather than the (read-only) store path.
      UV_PROJECT_ENVIRONMENT = venvDir;
      UV_CACHE_DIR = uvCacheDir;

      # Nix derives its cache path from $HOME/.cache/nix — it ignores
      # XDG_CACHE_HOME entirely. The service runs as root whose $HOME is
      # /root, which is read-only under ProtectSystem=strict. Redirecting
      # HOME to the writable StateDirectory fixes nix eval (module discovery)
      # and extra-container's internal nix invocations during container builds.
      HOME = "/var/lib/voxnix-agent";

      # XDG_CACHE_HOME is set as belt-and-suspenders for tools that respect
      # the XDG spec (e.g. some Python tooling). It does NOT fix the Nix
      # cache — that is solved by HOME above.
      XDG_CACHE_HOME = "/var/lib/voxnix-agent/cache";

      # extra-container uses NIX_PATH to resolve <nixpkgs/nixos> when building
      # containers. Without this, it fails inside the service's mount namespace
      # with "file 'nixpkgs/nixos' was not found in the Nix search path".
      # We point it directly at the nixpkgs store path from our flake inputs —
      # no channels needed, no mutable state.
      NIX_PATH = "nixpkgs=${inputs.nixpkgs}";

      # Prevent Python from writing .pyc files into the read-only store path.
      PYTHONDONTWRITEBYTECODE = "1";

      # Ensure Python output is unbuffered for real-time journalctl streaming.
      PYTHONUNBUFFERED = "1";
    };

    serviceConfig = {
      Type = "exec";

      # ── Secrets ──────────────────────────────────────────────────────────

      # agenix-decrypted environment file with all agent secrets:
      # TELEGRAM_BOT_TOKEN, LLM_PROVIDER, LLM_MODEL, <PROVIDER>_API_KEY,
      # and optionally LOGFIRE_TOKEN.
      EnvironmentFile = config.age.secrets.agent-env.path;

      # ── Working directory & state ────────────────────────────────────────

      # Work from the repo source in the store — uv reads pyproject.toml
      # and uv.lock from here.
      WorkingDirectory = "${voxnix-src}";

      # Persistent state directory at /var/lib/voxnix-agent/.
      # Houses the Python venv and uv download cache.
      StateDirectory = "voxnix-agent";

      # ── Startup ──────────────────────────────────────────────────────────

      # Install/update Python dependencies before starting the agent.
      # --frozen: use uv.lock exactly as committed (no resolution, no network
      #           lookups for version ranges — only downloads missing packages).
      # --no-dev: skip dev dependencies (pytest, ruff, ty) in production.
      # --no-editable: install the project itself as a regular package (not
      #                editable) since the source is in a read-only store path.
      #
      # This is a no-op when the venv is already up-to-date — adds <1s to
      # restart time in the common case.
      ExecStartPre = "${uv}/bin/uv sync --frozen --no-dev --no-editable";

      # Run the Telegram bot entry point.
      ExecStart = "${uv}/bin/uv run --frozen --no-dev --no-editable python -m agent.chat";

      # ── Restart policy ───────────────────────────────────────────────────

      # Restart on failure (crash, OOM, unhandled exception) but not on
      # clean exit (e.g. SIGTERM from systemd during shutdown).
      Restart = "on-failure";
      RestartSec = "10s";

      # ── Timeouts ─────────────────────────────────────────────────────────

      # uv sync may need to download packages on first run — allow up to
      # 5 minutes for the pre-start phase (generous for slow connections).
      TimeoutStartSec = 300;

      # Give the bot time to finish in-flight LLM calls before killing.
      TimeoutStopSec = 30;

      # ── Hardening ────────────────────────────────────────────────────────
      #
      # The agent runs as root because it needs direct access to:
      #   - machinectl (manages systemd-nspawn containers)
      #   - nixos-container (create/destroy containers)
      #   - extra-container (build + start declarative containers)
      #   - zfs (create/destroy datasets)
      #
      # These all require root or specific capabilities that are effectively
      # equivalent to root. For MVP, we run as root with basic hardening.
      # Future: explore CAP_SYS_ADMIN + polkit or a dedicated user with
      # targeted sudo rules.

      # Prevent the service from gaining new privileges via setuid/setgid.
      NoNewPrivileges = true;

      # Restrict access to /home, /root, and /run/user — agent doesn't need them.
      ProtectHome = true;

      # Mount /usr, /boot, /efi as read-only — agent only needs /nix/store
      # (already read-only) and /var/lib for state.
      ProtectSystem = "strict";

      # Directories the service needs write access to (beyond StateDirectory).
      # All paths must exist at service start — see systemd.tmpfiles.rules above.
      ReadWritePaths = [
        "/etc/systemd-mutable" # extra-container installs dynamic units here
        "/etc/nixos-containers" # extra-container symlinks <name>.conf here
        "/var/lib/nixos-containers" # nixos-container create/destroy
        "/var/lib/voxnix-agent" # own state (venv, cache)
        "/tank" # ZFS user datasets
        "/run" # systemd runtime, agenix secrets
        "/nix/var" # nix-daemon state (extra-container needs this)
        "/tmp" # uv sync + Python tempfile (extra-container also needs this)
        "/var/tmp" # fallback temp dir used by some Nix build tools
      ];

      # PrivateTmp is intentionally NOT set here. extra-container needs access
      # to the shared /tmp and /run for systemd unit installation. A private
      # /tmp namespace would isolate the temp .nix files from the systemd
      # machinery that extra-container uses to install container units.
    };
  };
}
