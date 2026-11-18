import json, os, py_compile, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fury import auth as A, system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.audit import Audit
from fury.webapp import Ctx, create_app

TOK = {r: A.new_token() for r in A.ROLES}
SPEC = ",".join(f"{r}-user:{r}:{A.hash_token(t)}" for r, t in TOK.items())


def make(demo=True, **kw):
    t, s, c = FakeTarget(), FakeSnow(), FakeChat(); bus, tr, _ = system.build(target=t, snow=s, chat=c)
    ctx = Ctx(bus, tr, s, c, "i-1", t, audit=Audit())
    return ctx, create_app(ctx, demo=demo, auth=A.Auth(A.parse_tokens(SPEC), **kw)).test_client()


def bearer(role): return {"Authorization": f"Bearer {TOK[role]}"}


class TokenTests(unittest.TestCase):
    def test_parse_rejects_malformed_entries(self):
        for bad in ("alice", "alice:root:" + "a" * 64, "alice:admin:short", "alice:admin:" + "z" * 64, ":admin:" + "a" * 64):
            with self.assertRaises(ValueError, msg=bad): A.parse_tokens(bad)
        self.assertEqual(A.parse_tokens(""), [])

    def test_config_holds_only_hashes_not_tokens(self):
        for t in TOK.values(): self.assertNotIn(t, SPEC)

    def test_cli_generates_matching_token_and_entry(self):
        out = subprocess.run([sys.executable, "-m", "fury.auth", "new", "carol", "operator"], cwd=ROOT, capture_output=True, text=True).stdout
        token = out.split("Token for carol (operator): ")[1].split()[0]; entry = out.strip().splitlines()[-1]
        self.assertEqual(entry, f"carol:operator:{A.hash_token(token)}"); self.assertEqual(len(A.parse_tokens(entry)), 1)

    def test_cli_rejects_bad_role(self):
        self.assertNotEqual(subprocess.run([sys.executable, "-m", "fury.auth", "new", "x", "root"], cwd=ROOT, capture_output=True).returncode, 0)


class RoleMapTests(unittest.TestCase):
    def test_required_roles(self):
        r = A.required_role
        self.assertIsNone(r("GET", "/")); self.assertEqual(r("GET", "/api/runs"), "viewer"); self.assertEqual(r("GET", "/api/health"), "viewer")
        self.assertEqual(r("POST", "/api/chat"), "operator"); self.assertEqual(r("POST", "/api/chat/reset"), "operator")
        for p in ("/api/audit", "/api/approvals/history", "/api/demo/inject/disk_full"): self.assertEqual(r("GET" if "demo" not in p else "POST", p), "admin")
        self.assertEqual(r("POST", "/api/anything-new"), "admin"); self.assertEqual(r("DELETE", "/api/runs"), "admin")   # deny by default


class EndpointTests(unittest.TestCase):
    def test_no_token_or_bad_token_is_401(self):
        _, c = make()
        self.assertEqual(c.get("/api/runs").status_code, 401)
        self.assertEqual(c.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.get("/api/runs", headers={"Authorization": TOK["admin"]}).status_code, 401)   # not a Bearer header

    def test_static_page_is_public(self): self.assertEqual(make()[1].get("/").status_code, 200)

    def test_viewer_reads_but_cannot_chat_or_see_audit(self):
        _, c = make()
        self.assertEqual(c.get("/api/runs", headers=bearer("viewer")).status_code, 200)
        self.assertEqual(c.post("/api/chat", json={"message": "hello"}, headers=bearer("viewer")).status_code, 403)
        self.assertEqual(c.get("/api/audit", headers=bearer("viewer")).status_code, 403)
        self.assertEqual(c.get("/api/approvals/history", headers=bearer("viewer")).status_code, 403)

    def test_operator_can_chat_but_not_admin_things(self):
        _, c = make()
        self.assertEqual(c.post("/api/chat", json={"message": "hello"}, headers=bearer("operator")).status_code, 200)
        self.assertEqual(c.get("/api/audit", headers=bearer("operator")).status_code, 403)
        self.assertEqual(c.post("/api/demo/inject/disk_full", headers=bearer("operator")).status_code, 403)

    def test_admin_can_do_everything(self):
        _, c = make()
        for path in ("/api/audit", "/api/approvals/history", "/api/obs/slo", "/api/health", "/api/runs"):
            self.assertEqual(c.get(path, headers=bearer("admin")).status_code, 200, path)
        self.assertEqual(c.post("/api/demo/reset", headers=bearer("admin")).status_code, 200)

    def test_denials_are_audited_without_the_token(self):
        ctx, c = make()
        c.get("/api/runs", headers={"Authorization": "Bearer SUPERSECRETGUESS"}); c.get("/api/audit", headers=bearer("viewer"))
        blob = json.dumps(ctx.audit.recent())
        self.assertNotIn("SUPERSECRETGUESS", blob); self.assertNotIn(TOK["viewer"], blob)
        self.assertEqual({(e["action"], e["outcome"]) for e in ctx.audit.recent()}, {("api.access", "denied")})
        self.assertIn("viewer-user", blob)

    def test_chat_report_audit_uses_the_authenticated_user(self):
        ctx, c = make()
        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            c.post("/api/chat", json={"message": "the disk is full"}, headers={**bearer("operator"), "X-Fury-Session": "s1"})
        self.assertIn(("operator-user", "chat.report", "allowed"), {(e["actor"], e["action"], e["outcome"]) for e in ctx.audit.recent()})

    def test_repeated_failures_are_throttled_then_recover(self):
        t = [0.0]; _, c = make(max_failures=3, window=60.0, clock=lambda: t[0])
        for _ in range(3): self.assertEqual(c.get("/api/runs").status_code, 401)
        self.assertEqual(c.get("/api/runs", headers=bearer("admin")).status_code, 429, "even a valid token is refused while throttled")
        t[0] = 61.0; self.assertEqual(c.get("/api/runs", headers=bearer("admin")).status_code, 200)

    def test_auth_disabled_leaves_api_open(self):
        t, s, ch = FakeTarget(), FakeSnow(), FakeChat(); bus, tr, _ = system.build(target=t, snow=s, chat=ch)
        self.assertEqual(create_app(Ctx(bus, tr, s, ch, "i-1", t), demo=True).test_client().get("/api/runs").status_code, 200)


class StartupTests(unittest.TestCase):
    def run_fury(self, extra_env, *args):
        d = tempfile.mkdtemp()      # empty cwd so a developer .env is never loaded
        env = {**os.environ, "PYTHONPATH": str(ROOT), "SNOW_URL": "https://example.invalid", "SNOW_USER": "u", "SNOW_PASSWORD": "p",
               "FURY_SNOW": "real", "FURY_TARGET": "fake", "FURY_CHAT": "fake", **extra_env}
        env.pop("FURY_API_TOKENS", None) if "FURY_API_TOKENS" not in extra_env else None
        return subprocess.run([sys.executable, str(ROOT / "run_fury.py"), *args], cwd=d, env=env, capture_output=True, text=True, timeout=20)

    def test_run_fury_compiles(self): py_compile.compile(str(ROOT / "run_fury.py"), doraise=True)

    def test_refuses_to_start_real_integrations_without_auth(self):
        r = self.run_fury({})
        self.assertNotEqual(r.returncode, 0); self.assertIn("Refusing to start", r.stderr); self.assertIn("python -m fury.auth new", r.stderr)

    def test_invalid_token_config_fails_closed(self):
        r = self.run_fury({"FURY_API_TOKENS": "alice:admin:notahash"})
        self.assertNotEqual(r.returncode, 0); self.assertIn("invalid FURY_API_TOKENS", r.stderr)


if __name__ == "__main__":
    unittest.main()
