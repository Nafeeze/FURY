import os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.store import Store
from fury.webapp import Ctx

ADMIN = "U0CAZH32V2T"

class StoreTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.path = os.path.join(d, "fury.db")

    def ctx(self, store, chat=None):
        t, s, c = FakeTarget(), FakeSnow(), chat or FakeChat()
        bus, triage, _ = system.build(target=t, snow=s, chat=c, store=store)
        return Ctx(bus, triage, s, c, "i-1", t, store=store)

    def wait(self, ctx, rid):
        for _ in range(200):
            if ctx.runs[rid]["state"] in ("done", "error"): return
            time.sleep(0.05)
        self.fail("run did not finish")

    def test_runs_survive_restart(self):
        c1 = self.ctx(Store(self.path)); rid = c1.answer("hello", "hi there")
        c2 = self.ctx(Store(self.path))
        self.assertIn(rid, c2.runs); self.assertEqual(c2.runs[rid]["out"]["message"], "hi there")

    def test_real_run_is_persisted_and_viewable_after_restart(self):
        c1 = self.ctx(Store(self.path))
        rid = c1.submit("handle_alert", system.ALERTS["disk_full"], "test", "disk")
        self.wait(c1, rid)
        c2 = self.ctx(Store(self.path)); v = c2.view(c2.runs[rid])
        self.assertEqual(v["state"], "done"); self.assertEqual(v["steps"], [])   # trace is in-memory only; summary survives

    def test_interrupted_runs_are_marked_failed_on_restart(self):
        st = Store(self.path); st.save_run({"id": "r1", "label": "x", "source": "chat", "state": "running", "start": 3, "end": None, "out": None, "at": "00:00:00"})
        c = self.ctx(Store(self.path))
        self.assertEqual(c.runs["r1"]["state"], "error"); self.assertEqual(c.runs["r1"]["out"]["reason"], "interrupted by restart")

    def test_attempt_counter_survives_restart(self):
        st = Store(self.path); st.record_attempt("INC1", "restart_service")
        self.assertEqual(Store(self.path).attempts("INC1", "restart_service"), 1)
        self.assertEqual(Store(self.path).attempts("INC1", "other"), 0)

    def test_second_remediation_blocked_after_restart(self):
        st1 = Store(self.path)
        bus1, _, _ = system.build(target=FakeTarget(), chat=FakeChat([(ADMIN, "approve")]), store=st1)
        inp = {"action": "restart_service", "target": "W3SVC", "ticket": "INC9", "summary": "s"}
        self.assertEqual(bus1.send("t", "remediate", inp, to="remediation").state, "completed")
        bus2, _, _ = system.build(target=FakeTarget(), chat=FakeChat([(ADMIN, "approve")]), store=Store(self.path))   # "restarted" process
        t2 = bus2.send("t", "remediate", inp, to="remediation")
        self.assertEqual(t2.state, "failed"); self.assertIn("already run once", t2.error)

    def test_no_store_keeps_old_behaviour(self):
        c = self.ctx(None); rid = c.answer("a", "b"); self.assertIn(rid, c.runs)

if __name__ == "__main__": unittest.main()
