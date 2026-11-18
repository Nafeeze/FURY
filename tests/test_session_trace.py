import json, os, sys, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system, telemetry
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.webapp import Ctx, create_app, session_ref

RAW_A, RAW_B = "raw-session-AAAA-1111", "raw-session-BBBB-2222"

class SessionTraceTests(unittest.TestCase):
    def setUp(self):
        t, s, c = FakeTarget(), FakeSnow(), FakeChat()
        bus, triage, _ = system.build(target=t, snow=s, chat=c)
        self.ctx = Ctx(bus, triage, s, c, "i-1", t)
        self.client = create_app(self.ctx, demo=True).test_client()

    def chat(self, msg, sid):
        r = self.client.post("/api/chat", json={"message": msg}, headers={"X-Fury-Session": sid})
        self.assertEqual(r.status_code, 200); return r.get_json()["run"]

    def test_session_ref_is_stable_short_and_not_the_raw_id(self):
        self.assertEqual(session_ref(RAW_A), session_ref(RAW_A)); self.assertNotEqual(session_ref(RAW_A), session_ref(RAW_B))
        self.assertEqual(len(session_ref(RAW_A)), 10); self.assertNotIn(RAW_A, session_ref(RAW_A))

    def test_runs_and_traces_group_by_session(self):
        a1, a2, b1 = self.chat("hello", RAW_A), self.chat("help", RAW_A), self.chat("hello", RAW_B)
        ra, rb = session_ref(RAW_A), session_ref(RAW_B)
        va = self.client.get(f"/api/obs/session/{ra}").get_json(); vb = self.client.get(f"/api/obs/session/{rb}").get_json()
        self.assertEqual({r["id"] for r in va["runs"]}, {a1, a2}); self.assertEqual({r["id"] for r in vb["runs"]}, {b1})
        self.assertTrue(va["traces"]); self.assertTrue(all(t["session"] == ra for t in va["traces"]))
        self.assertTrue({r["trace_id"] for r in va["runs"]} <= {t["trace_id"] for t in va["traces"]})

    def test_unknown_session_is_empty(self):
        v = self.client.get("/api/obs/session/deadbeef00").get_json()
        self.assertEqual((v["runs"], v["traces"]), ([], []))

    def test_raw_session_id_never_in_telemetry_or_responses(self):
        rid = self.chat("hello", RAW_A)
        blob = json.dumps(telemetry.traces(100)) + json.dumps(self.client.get(f"/api/run/{rid}").get_json()) \
             + json.dumps(self.client.get("/api/obs/overview").get_json()) \
             + json.dumps([telemetry.trace(t["trace_id"]) for t in telemetry.traces(100)], default=str)
        self.assertNotIn(RAW_A, blob)

    def test_incident_run_keeps_session_link(self):
        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            rid = self.chat("the disk is full", RAW_A)
        for _ in range(100):
            if self.ctx.runs[rid]["state"] in ("done", "error"): break
            time.sleep(0.05)
        self.assertEqual(self.client.get(f"/api/run/{rid}").get_json()["session"], session_ref(RAW_A))

if __name__ == "__main__": unittest.main()
