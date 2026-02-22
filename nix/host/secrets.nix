# Agenix integration — runtime secret decryption on the appliance.
#
# agenix decrypts .age files at boot using the host's SSH key, placing
# plaintext at /run/agenix/<name>. Secrets are never written to the
# Nix store or persisted to disk — they exist only in /run (tmpfs).
#
# The agent's systemd service reads secrets via EnvironmentFile
# (see agent-service.nix). Individual containers receive secrets
# through their own agenix declarations or bind-mounted files.
#
# Setup flow:
#   1. Admin encrypts secrets with their age key (see secrets/README.md)
#   2. After first provision, the host's SSH ed25519 key is added to
#      secrets/secrets.nix and all secrets are rekeyed (agenix --rekey)
#   3. On every boot, agenix decrypts using /etc/ssh/ssh_host_ed25519_key
#
# See docs/architecture.md § Secrets Management (agenix).
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # ── Identity ───────────────────────────────────────────────────────────────

  # agenix uses the host's SSH ed25519 key to decrypt secrets at boot.
  # This key is generated automatically by OpenSSH on first boot and
  # persists across rebuilds (stored in /etc/ssh/ on the ZFS var dataset).
  age.identityPaths = [
    "/etc/ssh/ssh_host_ed25519_key"
  ];

  # ── Secrets ────────────────────────────────────────────────────────────────

  # Combined environment file for the voxnix-agent systemd service.
  # Contains KEY=VALUE lines for all agent runtime secrets:
  #   TELEGRAM_BOT_TOKEN, LLM_PROVIDER, LLM_MODEL, <PROVIDER>_API_KEY,
  #   and optionally LOGFIRE_TOKEN.
  #
  # Decrypted to /run/agenix/agent-env (mode 0400, owner root).
  # Referenced by agent-service.nix via EnvironmentFile.
  age.secrets.agent-env = {
    file = ../../secrets/agent-env.age;

    # Only root needs to read the env file — systemd loads it before
    # dropping privileges (if we ever run the agent as a non-root user).
    owner = "root";
    group = "root";
    mode = "0400";
  };
}
