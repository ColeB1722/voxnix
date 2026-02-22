# agenix secret declarations — maps encrypted .age files to authorized keys.
#
# Each entry declares which age/SSH public keys can decrypt the secret.
# Two keys are needed:
#   - admin: your personal age key (on your MacBook) — lets you encrypt/edit secrets
#   - appliance: the NixOS VM's SSH host key — lets the appliance decrypt secrets at boot
#
# Setup:
#   1. Generate an age key:  age-keygen -o ~/.config/age/voxnix.txt
#   2. Paste your public key below (replacing the placeholder)
#   3. Encrypt secrets:  cd secrets && agenix -e agent-env.age
#   4. After first provision, add the appliance's SSH public key:
#        ssh-keyscan -t ed25519 <vm-ip> 2>/dev/null
#      Paste it below, re-key all secrets:  agenix --rekey
#
# See README.md § Step 5 for the full walkthrough.

let
  # ── Authorized keys ──────────────────────────────────────────────────────
  #
  # Replace these placeholders with real keys before encrypting secrets.

  # Your personal age public key (on your MacBook / deployment machine).
  # This lets you encrypt and edit secrets from your laptop.
  # Generate with: age-keygen -o ~/.config/age/voxnix.txt
  admin = "age13k6t6ww23ryatnnda755lk0ksrpc6vv3sd79ry7mh9q8vmgcl40q85ya5u";

  # The NixOS appliance's SSH public key (the Hyper-V VM, not your MacBook).
  # This lets the appliance decrypt its own secrets at boot.
  # Retrieve after first provision: ssh-keyscan -t ed25519 <vm-ip> 2>/dev/null
  # Leave as empty string until the appliance is provisioned, then rekey.
  appliance = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG6Si8Z/mpGKgtPAIJzxrv1qhYrcdZWEY/wwDelRiSr0";

  # All keys that should be able to decrypt secrets.
  # Before first provision: just the admin key.
  # After first provision: admin + appliance (so the VM can decrypt at boot).
  allKeys = [ admin ] ++ (if appliance != "" then [ appliance ] else [ ]);

in
{
  # Combined environment file for the voxnix-agent systemd service.
  # Contains all runtime secrets as KEY=VALUE lines:
  #
  #   TELEGRAM_BOT_TOKEN=...
  #   LLM_PROVIDER=anthropic
  #   LLM_MODEL=claude-sonnet-4-20250514
  #   ANTHROPIC_API_KEY=sk-ant-...
  #   LOGFIRE_TOKEN=...          (optional — omit line to disable)
  #
  # Encrypt with:  agenix -e agent-env.age
  "agent-env.age".publicKeys = allKeys;
}
