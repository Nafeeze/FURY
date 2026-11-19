import json, unittest
from fury import secrets


class Stub:
    def __init__(self, payload): self.payload = payload
    def get_secret_value(self, SecretId): return {"SecretString": self.payload if isinstance(self.payload, str) else json.dumps(self.payload)}


class SecretsTests(unittest.TestCase):
    def test_env_provider_does_nothing(self):
        env = {}; self.assertEqual(secrets.load(env, Stub({})), []); self.assertEqual(env, {})

    def test_applies_allowed_keys_without_overwriting_real_env(self):
        env = {"FURY_SECRETS_PROVIDER": "aws", "FURY_SECRETS_ID": "x", "SNOW_USER": "from-env"}
        got = secrets.load(env, Stub({"SNOW_USER": "from-secret", "SNOW_PASSWORD": "pw", "SLACK_BOT_TOKEN": "t"}))
        self.assertEqual(env["SNOW_USER"], "from-env"); self.assertEqual(env["SNOW_PASSWORD"], "pw"); self.assertEqual(sorted(got), ["SLACK_BOT_TOKEN", "SNOW_PASSWORD"])

    def test_cannot_inject_arbitrary_environment_variables(self):
        env = {"FURY_SECRETS_PROVIDER": "aws", "FURY_SECRETS_ID": "x"}
        secrets.load(env, Stub({"PATH": "/evil", "PYTHONPATH": "/evil", "LD_PRELOAD": "x", "SNOW_URL": "https://ok"}))
        self.assertEqual({k for k in env} - {"FURY_SECRETS_PROVIDER", "FURY_SECRETS_ID"}, {"SNOW_URL"})

    def test_failure_never_echoes_the_secret_payload(self):
        env = {"FURY_SECRETS_PROVIDER": "aws", "FURY_SECRETS_ID": "x"}
        with self.assertRaises(RuntimeError) as cm: secrets.load(env, Stub("not-json SECRETVALUE123"))
        self.assertNotIn("SECRETVALUE123", str(cm.exception))

    def test_misconfiguration_is_explicit(self):
        with self.assertRaises(ValueError): secrets.load({"FURY_SECRETS_PROVIDER": "vault"})
        with self.assertRaises(ValueError): secrets.load({"FURY_SECRETS_PROVIDER": "aws"})


if __name__ == "__main__": unittest.main()
