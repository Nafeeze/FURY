"""Secrets provider: load FURY's secrets from AWS Secrets Manager instead of a `.env` file on disk.

    FURY_SECRETS_PROVIDER=aws   FURY_SECRETS_ID=fury/prod   (secret value = JSON object of KEY: value)

Precedence: anything already set (real environment, then `.env`) wins; the secrets manager only fills in what is missing.
In production keep `.env` to the two FURY_SECRETS_* lines and put everything else in the secret.
Only an allow-list of key prefixes is accepted, so a compromised or mistaken secret cannot inject arbitrary environment
variables such as PATH or PYTHONPATH. Values are never logged. Uses the host's own AWS identity (instance role), not
keys from a file. NOTE: unit-tested with a stub client; not yet exercised against a live AWS account."""
import json, os

ALLOWED_PREFIXES = ("SNOW_", "AWS_DIAG_", "AWS_REMED_", "SLACK_", "APPROVAL_", "FURY_API_TOKENS", "NVIDIA_", "OPENAI_", "ANTHROPIC_")


def load(env=None, client=None):
    """Apply secrets to `env` (default os.environ) without overwriting anything already set. Returns the key names applied."""
    env = os.environ if env is None else env
    provider = (env.get("FURY_SECRETS_PROVIDER") or "env").strip().lower()
    if provider == "env": return []
    if provider != "aws": raise ValueError(f"unknown FURY_SECRETS_PROVIDER '{provider}' (use 'env' or 'aws')")
    secret_id = env.get("FURY_SECRETS_ID")
    if not secret_id: raise ValueError("FURY_SECRETS_PROVIDER=aws needs FURY_SECRETS_ID")
    if client is None:
        import boto3
        client = boto3.client("secretsmanager", region_name=env.get("AWS_REGION") or None)
    try: data = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    except Exception as e: raise RuntimeError(f"could not read secret '{secret_id}': {type(e).__name__}") from None   # never echo the payload
    if not isinstance(data, dict): raise ValueError("the secret must be a JSON object of KEY: value")
    applied = []
    for k, v in data.items():
        if not isinstance(v, (str, int, float)) or not k.startswith(ALLOWED_PREFIXES): continue
        if k not in env: env[k] = str(v); applied.append(k)
    return applied
