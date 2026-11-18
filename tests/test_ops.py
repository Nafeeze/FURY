import json, os, sqlite3, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import slo, system, telemetry
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.audit import Audit, GENESIS
from fury.store import Store
from fury.webapp import Ctx, create_app


def tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


class AuditTests(unittest.TestCase):
    def test_chain_links_and_verifies(self):
        p = tmp("a.jsonl"); a = Audit(p)
        e1 = a.record("alice", "chat.report", "allowed", text_len=10); e2 = a.record("bob", "approve", "denied", reason="not approver")
        self.assertEqual(e1["prev"], GENESIS); self.assertEqual(e2["prev"], e1["hash"]); self.assertEqual(a.head, e2["hash"])
        self.assertEqual(Audit.verify_file(p), (True, 2, None))

    def test_tampering_is_detected(self):
        p = tmp("a.jsonl"); a = Audit(p)
        for i in range(4): a.record("u", "act", "ok", n=i)
        lines = Path(p).read_text(encoding="utf-8").splitlines()
        edited = json.loads(lines[1]); edited["outcome"] = "denied"; lines[1] = json.dumps(edited, sort_keys=True)
        Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(Audit.verify_file(p), (False, 2, 2))

    def test_deleting_or_reordering_is_detected(self):
        p = tmp("a.jsonl"); a = Audit(p)
        for i in range(4): a.record("u", "act", "ok", n=i)
        lines = Path(p).read_text(encoding="utf-8").splitlines()
        Path(p).write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
        self.assertFalse(Audit.verify_file(p)[0])
        Path(p).write_text("\n".join([lines[1], lines[0]] + lines[2:]) + "\n", encoding="utf-8")
        self.assertFalse(Audit.verify_file(p)[0])

    def test_reopening_continues_the_chain(self):
        p = tmp("a.jsonl"); Audit(p).record("u", "a", "ok"); b = Audit(p); b.record("u", "b", "ok")
        self.assertEqual(Audit.verify_file(p), (True, 2, None))

    def test_secrets_in_detail_are_redacted(self):
        e = Audit().record("u", "x", "ok", note="password=hunter2 key AKIAABCDEFGHIJKLMNOP")
        self.assertNotIn("hunter2", json.dumps(e)); self.assertNotIn("AKIAABCDEFGHIJKLMNOP", json.dumps(e))

    def test_chat_report_decisions_are_audited_without_raw_session_id(self):
        t, s, c = FakeTarget(), FakeSnow(), FakeChat(); bus, tr, _ = system.build(target=t, snow=s, chat=c)
        ctx = Ctx(bus, tr, s, c, "i-1", t); client = create_app(ctx, demo=True).test_client()
        client.post("/api/chat", json={"message": "the disk is full"}, headers={"X-Fury-Session": "RAW-SESSION-XYZ"})
        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            client.post("/api/chat", json={"message": "the disk is full"}, headers={"X-Fury-Session": "RAW-SESSION-ABC"})
        d = client.get("/api/audit").get_json(); self.assertEqual({(e["action"], e["outcome"]) for e in d["events"]}, {("chat.report", "denied"), ("chat.report", "allowed")})
        self.assertNotIn("RAW-SESSION", json.dumps(d)); self.assertEqual(d["head"], ctx.audit.head)


class SloTests(unittest.TestCase):
    def setUp(self): telemetry.reset()

    def test_slo_shape_and_no_data_is_never_ok(self):
        r = slo.evaluate()
        self.assertEqual(r["status"], "no_data", "no evidence must never read as healthy")
        self.assertEqual({s["id"] for s in r["slos"]}, {"workflow_completion", "escalation_rate", "agent_hop_p95", "integration_errors", "contract_violations"})
        for s in r["slos"]: self.assertIn(s["status"], ("ok", "breach", "no_data"))

    def test_breach_is_reported_when_budget_stops_workflows(self):
        t = FakeTarget(); t.inject("disk_full"); bus, _, _ = system.build(target=t, snow=FakeSnow(), chat=FakeChat([("U0CAZH32V2T", "approve")]))
        with patch.dict(os.environ, {"FURY_MAX_WORKFLOW_HOPS": "3"}):
            for _ in range(3): bus.send("cw", "handle_alert", system.ALERTS["disk_full"], to="triage")
        r = slo.evaluate(); self.assertIn("workflow_completion", r["breaches"]); self.assertEqual(r["status"], "breach")

    def test_endpoint(self):
        t, s, c = FakeTarget(), FakeSnow(), FakeChat(); bus, tr, _ = system.build(target=t, snow=s, chat=c)
        d = create_app(Ctx(bus, tr, s, c, "i-1", t), demo=True).test_client().get("/api/obs/slo").get_json()
        self.assertIn(d["status"], ("ok", "breach", "no_data")); self.assertTrue(d["slos"])


class RetentionTests(unittest.TestCase):
    def test_prune_removes_only_old_records(self):
        p = tmp("f.db"); st = Store(p)
        st.save_run({"id": "old", "state": "done", "at": "x", "out": None}); st.save_run({"id": "new", "state": "done", "at": "x", "out": None})
        a_old, a_new = st.approval_open("T1", "restart_service", "W3SVC", "human"), st.approval_open("T2", "restart_service", "W3SVC", "human")
        db = sqlite3.connect(p); db.execute("UPDATE runs SET updated = julianday('now') - 200 WHERE id='old'")
        db.execute("UPDATE approvals SET requested_at = datetime('now','-200 days') WHERE id=?", (a_old,)); db.commit(); db.close()
        self.assertEqual(st.prune(90), {"runs": 1, "approvals": 1})
        self.assertEqual(set(st.load_runs()), {"new"}); self.assertEqual([h["ticket"] for h in st.approvals_history()], ["T2"])

    def test_zero_days_keeps_everything(self):
        st = Store(tmp("f.db")); st.save_run({"id": "r", "state": "done", "at": "x", "out": None})
        self.assertEqual(st.prune(0), {"runs": 0, "approvals": 0}); self.assertIn("r", st.load_runs())


if __name__ == "__main__":
    unittest.main()
