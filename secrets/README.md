# Secrets

This directory contains [agenix](https://github.com/ryantm/agenix) encrypted secrets for the voxnix appliance.

## Files

| File | Purpose |
|------|---------|
| `secrets.nix` | Declares which age/SSH public keys can decrypt each secret |
| `agent-env.age` | Encrypted environment file for the voxnix-agent systemd service |

## Setup

### 1. Generate your age key (once, on your deployment machine)

```bash
age-keygen -o ~/.config/age/voxnix.txt
# prints: Public key: age1...
```

### 2. Add your public key to `secrets.nix`

Replace the `admin` placeholder with the public key printed above.

### 3. Create the encrypted environment file

```bash
cd secrets
agenix -e agent-env.age
```

Your editor will open. Paste the following (with your real values):

```
TELEGRAM_BOT_TOKEN=1234567:ABCdefGHIjklMNOpqrSTUvwxYZ
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-api03-...
LOGFIRE_TOKEN=your-logfire-token
```

**Notes:**
- `LLM_PROVIDER` must match a pydantic-ai provider identifier (`anthropic`, `openai`, `google`, etc.)
- The API key variable name must match the provider: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, etc.
- `LOGFIRE_TOKEN` is optional — omit the line entirely to run without remote tracing.

### 4. Commit the encrypted file

The `.age` file is safe to commit — only your age key can decrypt it.

```bash
git add agent-env.age
git commit -m "secrets: add encrypted agent environment"
```

### 5. After first provision — add the host key

Once the appliance is provisioned, retrieve its SSH host key:

```bash
ssh-keyscan -t ed25519 <vm-ip> 2>/dev/null
```

Paste the full `ssh-ed25519 AAAA...` line as the `host` value in `secrets.nix`, then re-encrypt all secrets so the appliance can decrypt them at boot:

```bash
agenix --rekey
git add -A && git commit -m "secrets: rekey with host key"
just deploy <vm-ip>
```

## Rotating a secret

Edit the encrypted file and redeploy:

```bash
agenix -e agent-env.age   # opens your editor with decrypted content
just deploy <vm-ip>        # deploys updated config, restarts the agent
```
