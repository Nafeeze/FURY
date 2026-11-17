import sys, time, unittest, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system, telemetry
from fury.adapters import FakeTarget, FakeSnow, FakeChat
from fury.webapp import Ctx, create_app

ADMIN = "U0CAZH32V2T"

def run_chat(msg, fault=None, script=None):
    t = FakeTarget(); snow = FakeSnow(); chat = FakeChat(script)
    if fault: t.inject(fault)
    bus, triage, env = system.build(target=t, snow=snow, chat=chat)
    c = create_app(Ctx(bus, triage, snow, chat, "i-1", t), demo=True).test_client()
    rid = c.post("/api/chat", json={"message": msg}).get_json()["run"]
    for _ in range(100):
        v = c.get("/api/run/" + rid).get_json()
        if v["state"] in ("done", "error"): break
        time.sleep(0.05)
    return c, v

class TraceTests(unittest.TestCase):
    def test_one_request_is_one_connected_trace(self):
        c, v = run_chat("disk F is full again", "disk_full", [(ADMIN, "approve")])
        tr = c.get("/api/obs/trace/" + v["trace_id"]).get_json()
        names = [s["name"] for s in tr["spans"]]
        for n in ("chat.request", "run", "retrieval.runbook", "tool.call", "agent.decision", "approval.wait"):
            self.assertIn(n, names)
        ids = {s["span_id"] for s in tr["spans"]}
        roots = [s for s in tr["spans"] if s["parent_id"] is None]
        self.assertEqual(len(roots), 1)                                   # exactly one root
        self.assertTrue(all(s["parent_id"] in ids for s in tr["spans"] if s["parent_id"]))  # no orphans
        self.assertEqual({s["trace_id"] for s in tr["spans"]}, {v["trace_id"]})

    def test_failed_approval_is_visible_in_trace(self):
        c, v = run_chat("disk is full on F:", "disk_full", [(ADMIN, "reject")])
        tr = c.get("/api/obs/trace/" + v["trace_id"]).get_json()
        ap = next(s for s in tr["spans"] if s["name"] == "approval.wait")
        self.assertFalse(ap["ok"]); self.assertEqual(ap["attrs"]["decision"], "rejected")
        self.assertTrue(any(s["name"] == "agent.hop" and s["attrs"].get("state") == "failed" for s in tr["spans"]))

    def test_no_runbook_shows_retrieval_miss(self):
        c, v = run_chat("memory usage is high", "memory_pressure")
        tr = c.get("/api/obs/trace/" + v["trace_id"]).get_json()
        r = next(s for s in tr["spans"] if s["name"] == "retrieval.runbook")
        self.assertFalse(r["attrs"]["hit"])

    def test_chat_reply_has_trace_without_message_text(self):
        c, v = run_chat("contact jane.doe@example.com about 10.0.4.17 weather")
        dump = json.dumps(c.get("/api/obs/trace/" + v["trace_id"]).get_json())
        self.assertNotIn("jane.doe@example.com", dump); self.assertNotIn("10.0.4.17", dump)
        self.assertNotIn("weather", dump)   # the message text itself is never stored on the root span

    def test_overview_shape(self):
        c, _ = run_chat("hello")
        o = c.get("/api/obs/overview").get_json()
        for k in ("backend", "kpis", "signals", "traces", "metrics", "retention"):
            self.assertIn(k, o)
        for s in o["signals"]:
            self.assertIn(s["level"], ("act", "ok", "idle")); self.assertTrue(s["owner"] and s["action"])

if __name__ == "__main__":
    unittest.main()
