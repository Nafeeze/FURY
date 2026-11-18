import os, sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system
from fury.a2a import Budget, RESERVE_SKILLS
from fury.adapters import FakeChat, FakeSnow, FakeTarget

ADMIN = "U0CAZH32V2T"

def run(fault="disk_full", env=None, script=((ADMIN, "approve"),)):
    t, s, c = FakeTarget(), FakeSnow(), FakeChat(script); t.inject(fault)
    bus, _, _ = system.build(target=t, snow=s, chat=c)
    with patch.dict(os.environ, env or {}):
        out = bus.send("cloudwatch", "handle_alert", system.ALERTS[fault], to="triage")
    return out, bus, t, s, c

class BudgetUnitTests(unittest.TestCase):
    def test_hop_limit(self):
        b = Budget(max_hops=2)
        self.assertIsNone(b.admit("diagnostic", "run_checks")); self.assertIsNone(b.admit("knowledge", "match_runbook"))
        self.assertIn("hop limit", b.admit("diagnostic", "run_checks"))

    def test_per_agent_limit(self):
        b = Budget(per_agent={"remediation": 1})
        self.assertIsNone(b.admit("remediation", "remediate")); self.assertIn("remediation call limit", b.admit("remediation", "remediate"))

    def test_time_limit(self):
        self.assertIn("time limit", Budget(max_seconds=-1).admit("knowledge", "match_runbook"))

    def test_escalation_skills_survive_exhaustion_up_to_reserve(self):
        b = Budget(max_hops=0, max_reserve=2)
        self.assertIn("hop limit", b.admit("knowledge", "match_runbook"))
        for s in sorted(RESERVE_SKILLS)[:2]: self.assertIsNone(b.admit("itsm", s))
        self.assertIn("reserve", b.admit("itsm", "add_note"))

    def test_resolve_and_remediate_are_not_reserved(self):
        self.assertTrue({"resolve", "remediate", "request_approval"}.isdisjoint(RESERVE_SKILLS))

class BudgetWorkflowTests(unittest.TestCase):
    def test_normal_incident_fits_comfortably(self):
        out, bus, *_ = run()
        self.assertEqual(out.output["outcome"], "resolved"); self.assertLess(len(bus.trace), 20)   # limit is 40

    def test_hop_limit_stops_fix_and_escalates_to_human(self):
        out, bus, target, snow, chat = run(env={"FURY_MAX_WORKFLOW_HOPS": "3"})
        o = out.output
        self.assertEqual(o["outcome"], "escalated"); self.assertIn("budget", o["reason"])
        self.assertEqual(target.executed, [], "no action may run once the budget is gone")
        self.assertTrue(any("Escalation" in m[1] for m in chat.messages), "a human must be told")
        self.assertTrue(any(h["to"] == "remediation" and h["state"] == "failed" for h in bus.trace), "the over-budget hop is refused, not run")
        self.assertIn("ESCALATED", " ".join(n["text"] for n in snow.get_notes(o["ticket"])))

    def test_time_limit_escalates_even_before_a_ticket_exists(self):
        out, _, target, _, chat = run(env={"FURY_MAX_WORKFLOW_SECONDS": "-1"})
        self.assertEqual(out.output["outcome"], "escalated"); self.assertIsNone(out.output["ticket"])
        self.assertEqual(target.executed, []); self.assertTrue(any("Escalation" in m[1] for m in chat.messages))

    def test_each_workflow_gets_a_fresh_budget(self):
        t, s, c = FakeTarget(), FakeSnow(), FakeChat([(ADMIN, "approve")] * 3)
        bus, _, _ = system.build(target=t, snow=s, chat=c)
        with patch.dict(os.environ, {"FURY_MAX_WORKFLOW_HOPS": "14"}):
            for fault in ("disk_full", "service_down", "cpu_runaway"):
                t.inject(fault); r = bus.send("cw", "handle_alert", system.ALERTS[fault], to="triage")
                self.assertEqual(r.output["outcome"], "resolved", fault); t.reset()

    def test_direct_sends_outside_a_workflow_are_unbudgeted(self):
        bus, _, _ = system.build(target=FakeTarget(), chat=FakeChat())
        with patch.dict(os.environ, {"FURY_MAX_WORKFLOW_HOPS": "0"}):
            for _ in range(5): self.assertEqual(bus.send("t", "match_runbook", {"query": "disk full"}, to="knowledge").state, "completed")

    def test_budget_exceeded_is_counted(self):
        from fury import telemetry
        run(env={"FURY_MAX_WORKFLOW_HOPS": "3"})
        self.assertIn("budget.exceeded", str(telemetry.summary()))

if __name__ == "__main__": unittest.main()
