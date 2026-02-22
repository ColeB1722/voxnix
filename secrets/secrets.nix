# agenix secret declarations — maps encrypted .age files to authorized keys.
#
# Each entry declares which age/SSH public keys can decrypt the secret.
# The admin key is your personal age key (from age-keygen).
# The host key is the appliance's SSH host key (added after first provision).
#
# Setup:
#   1. Generate an age key:  age-keygen -o ~/.config/age/voxnix.txt
#   2. Paste your public key below (replacing the placeholder)
#   3. Encrypt secrets:  cd secrets && agenix -e agent-env.age
#   4. After first provision, add the host's SSH public key:
#        ssh-keyscan <vm-ip> 2>/dev/null | grep ed25519
#      Paste it below, re-key all secrets:  agenix --rekey
#
# See README.md § Step 5 for the full walkthrough.

let
  # ── Authorized keys ──────────────────────────────────────────────────────
  #
  # Replace these placeholders with real keys before encrypting secrets.

  # Admin's age public key (from ~/.config/age/voxnix.txt)
  # Generate with: age-keygen -o ~/.config/age/voxnix.txt
  admin = "age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";

  # Appliance SSH host key (added after first provision)
  # Retrieve with: ssh-keyscan <vm-ip> 2>/dev/null | grep ed25519
  # Leave as empty string until the host is provisioned, then rekey.
  host = "";

  # All keys that should be able to decrypt secrets.
  # Before first provision: just the admin key.
  # After first provision: admin + host (so the appliance can decrypt at boot).
  allKeys = [ admin ] ++ (if host != "" then [ host ] else [ ]);

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
