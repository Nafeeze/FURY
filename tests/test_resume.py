"""Workflow resume after a restart: continue from a saved checkpoint, never repeat an action, never guess."""
import sys, time, unittest, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system, a2a
from fury.adapters import FakeTarget, FakeSnow, FakeChat
from fury.store import Store
from fury.system import ALERTS
from fury.webapp import Ctx

ADMIN = "U0CAZH32V2T"

def ticket_for(snow, alert):
    n = snow.create({"short_description": f"[{alert['name']} {alert['instance']}] {alert['description']}", "description": "x"}); snow.claim(n); return n

class TriageResume(unittest.TestCase):
    def test_resume_before_the_fix_reuses_the_ticket_asks_approval_and_resolves(self):
        t = FakeTarget(); t.inject("service_down"); s = FakeSnow(); c = FakeChat([(ADMIN, "approve")])
        bus, triage, env = system.build(target=t, snow=s, chat=c)
        tk = ticket_for(s, ALERTS["service_down"])
        out = bus.send("cloudwatch", "handle_alert", dict(ALERTS["service_down"], _resume={"ticket": tk}), to="triage").output
        self.assertEqual(out["outcome"], "resolved"); self.assertEqual(out["ticket"], tk)
        self.assertEqual(len(s.incidents), 1, "a resumed run must not open a second ticket")
        self.assertEqual([a for a, _ in t.executed], ["restart_service"])
        self.assertIn("resuming", "\n".join(s.incidents[tk]["work_notes"]))

    def test_resume_after_the_fix_only_verifies_and_never_repeats_the_action(self):
        t = FakeTarget(); t.inject("service_down"); s = FakeSnow(); c = FakeChat([])   # nobody to approve: any new approval request would escalate
        bus, triage, env = system.build(target=t, snow=s, chat=c)
        tk = ticket_for(s, ALERTS["service_down"])
        t.act("restart_service", "W3SVC"); t.executed.clear()                      # the fix ran before the restart
        cp = {"ticket": tk, "acted": True, "approval": {"approver": ADMIN, "approved_at": "2026-11-19 13:35:16", "result": {"state": "running"}}}
        out = bus.send("cloudwatch", "handle_alert", dict(ALERTS["service_down"], _resume=cp), to="triage").output
        self.assertEqual(out["outcome"], "resolved"); self.assertEqual(t.executed, [])
        self.assertEqual(c.requests, [], "no second approval may be requested")
        self.assertIn(ADMIN, s.incidents[tk]["close_notes"])

    def test_fault_gone_after_restart_without_a_fix_goes_to_a_human(self):
        t = FakeTarget(); s = FakeSnow(); bus, triage, env = system.build(target=t, snow=s, chat=FakeChat([]))
        tk = ticket_for(s, ALERTS["service_down"])
        out = bus.send("cloudwatch", "handle_alert", dict(ALERTS["service_down"], _resume={"ticket": tk}), to="triage").output
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(t.executed, []); self.assertNotEqual(s.incidents[tk]["state"], "6")

    def test_checkpoints_are_emitted_in_order(self):
        t = FakeTarget(); t.inject("service_down"); bus, triage, env = system.build(target=t, chat=FakeChat([(ADMIN, "approve")]))
        seen = []; triage.checkpoint = lambda **kw: seen.append(sorted(kw))
        bus.send("cloudwatch", "handle_alert", ALERTS["service_down"], to="triage")
        self.assertEqual(seen, [["ticket"], ["acted", "approval"]])

class InflightGuard(unittest.TestCase):
    def test_unknown_outcome_is_never_repeated(self):
        st = Store(); t = FakeTarget(); t.inject("service_down"); s = FakeSnow(); c = FakeChat([(ADMIN, "approve")])
        bus, triage, env = system.build(target=t, snow=s, chat=c, store=st)
        tk = ticket_for(s, ALERTS["service_down"]); st.mark_inflight(tk, "restart_service")   # the process died while the action was being sent
        out = bus.send("cloudwatch", "handle_alert", dict(ALERTS["service_down"], _resume={"ticket": tk}), to="triage").output
        self.assertEqual(out["outcome"], "escalated"); self.assertIn("unknown", out["reason"]); self.assertEqual(t.executed, [])
        self.assertEqual(st.approvals_history()[0]["status"], "denied")

    def test_marker_is_cleared_after_success_and_after_failure(self):
        st = Store(); t = FakeTarget(fix_works=False); t.inject("disk_full")
        bus, _, _ = system.build(target=t, chat=FakeChat([(ADMIN, "approve")]), store=st)
        bus.send("cloudwatch", "handle_alert", ALERTS["disk_full"], to="triage")
        self.assertFalse(st.inflight("INC0000001", "clear_temp_files"))

class StoreRecovery(unittest.TestCase):
    def _run(self, **kw): return dict({"id": "r1", "label": "x", "source": "cloudwatch", "state": "running", "out": None, "at": "10:00:00",
                                       "ts": time.time(), "resumes": 0, "input": {"skill": "handle_alert", "data": {}, "source": "cloudwatch"}}, **kw)
    def test_default_still_fails_interrupted_runs(self):
        st = Store(); st.save_run(self._run()); self.assertEqual(st.recover_interrupted(), 1)
        self.assertEqual(st.load_runs()["r1"]["state"], "error"); self.assertNotIn("input", st.load_runs()["r1"])
    def test_recent_run_with_input_is_marked_interrupted(self):
        st = Store(); st.save_run(self._run()); self.assertEqual(st.recover_interrupted(1800), 0)
        self.assertEqual(st.load_runs()["r1"]["state"], "interrupted")
    def test_stale_or_already_resumed_runs_fail(self):
        st = Store(); st.save_run(self._run(ts=time.time() - 7200)); st.save_run(self._run(id="r2", resumes=1)); st.save_run(self._run(id="r3", input=None))
        self.assertEqual(st.recover_interrupted(1800), 3)
        self.assertTrue(all(r["state"] == "error" for r in st.load_runs().values()))

class OrchestratorRestart(unittest.TestCase):
    def test_crash_mid_run_then_restart_resumes_and_resolves(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "fury.db")
        t = FakeTarget(); t.inject("service_down"); s = FakeSnow(); tk = ticket_for(s, ALERTS["service_down"])
        # --- process 1 dies after opening the ticket: this is exactly what was persisted at that moment
        st1 = Store(path)
        st1.save_run({"id": "abc12345", "label": "W3SVC service stopped", "source": "cloudwatch", "state": "running", "start": 3, "end": None, "out": None, "at": "13:30:00",
                      "ts": time.time(), "resumes": 0, "input": {"skill": "handle_alert", "data": ALERTS["service_down"], "source": "cloudwatch"}, "cp": {"ticket": tk}})
        st1.close()
        # --- process 2 starts
        st2 = Store(path); bus, triage, env = system.build(target=t, snow=s, chat=FakeChat([(ADMIN, "approve")]), store=st2)
        ctx = Ctx(bus, triage, s, env["chat"], "i-1", None, store=st2)
        self.assertEqual(ctx.runs["abc12345"]["state"], "interrupted")
        self.assertEqual(ctx.resume_interrupted(), ["abc12345"])
        for _ in range(100):
            if ctx.runs["abc12345"]["state"] in ("done", "error"): break
            time.sleep(0.05)
        r = ctx.runs["abc12345"]
        self.assertEqual((r["state"], r["out"]["outcome"], r["out"]["ticket"]), ("done", "resolved", tk))
        self.assertEqual(len(s.incidents), 1); self.assertEqual(r["resumes"], 1)
        self.assertNotIn("input", st2.load_runs()["abc12345"], "saved input is dropped once the run ends")
        st2.close()

if __name__ == "__main__": unittest.main()
