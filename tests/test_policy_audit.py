import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import policy, system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.store import Store

ADMIN, INP = "U0CAZH32V2T", {"action": "restart_service", "target": "W3SVC", "ticket": "INC1", "summary": "s"}

def build(script=(), store=None):
    t = FakeTarget(); bus, _, _ = system.build(target=t, chat=FakeChat(list(script)), store=store); return bus, t

class PolicyTests(unittest.TestCase):
    def test_catalog_actions_default_to_human_and_unknown_is_restricted(self):
        for a in ("restart_service", "clear_temp_files", "kill_process"): self.assertEqual(policy.tier(a), policy.HUMAN)
        self.assertEqual(policy.tier("delete_volume"), policy.RESTRICTED); self.assertFalse(policy.allowed("delete_volume"))

    def test_config_can_restrict_or_enable_policy_tier_but_never_loosen_unknown(self):
        with patch.dict(os.environ, {"FURY_ACTION_TIERS": "kill_process=restricted,clear_temp_files=policy,delete_volume=policy"}):
            self.assertEqual(policy.tier("kill_process"), policy.RESTRICTED); self.assertEqual(policy.tier("clear_temp_files"), policy.POLICY)
            self.assertEqual(policy.tier("delete_volume"), policy.RESTRICTED)

    def test_restricted_action_is_refused_without_asking_or_acting(self):
        bus, t = build([(ADMIN, "approve")])
        with patch.dict(os.environ, {"FURY_ACTION_TIERS": "restart_service=restricted"}):
            r = bus.send("t", "remediate", INP, to="remediation")
        self.assertEqual(r.state, "failed"); self.assertIn("restricted", r.error); self.assertEqual(t.executed, [])

    def test_human_tier_without_approval_does_not_act(self):
        bus, t = build([])
        self.assertEqual(bus.send("t", "remediate", INP, to="remediation").state, "failed"); self.assertEqual(t.executed, [])

    def test_policy_tier_runs_without_a_human_but_is_audited(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "f.db")); bus, t = build([], store=st)
        inp = {**INP, "action": "clear_temp_files", "target": r"F:\fury-tmp"}
        with patch.dict(os.environ, {"FURY_ACTION_TIERS": "clear_temp_files=policy"}):
            r = bus.send("t", "remediate", inp, to="remediation")
        self.assertEqual(r.state, "completed"); self.assertEqual(r.output["approver"], "policy"); self.assertTrue(t.executed)
        h = st.approvals_history()[0]; self.assertEqual((h["status"], h["tier"], h["approver"]), ("executed", "policy", "policy"))

class AuditTests(unittest.TestCase):
    def setUp(self): self.st = Store(os.path.join(tempfile.mkdtemp(), "f.db"))

    def test_approved_and_executed_is_recorded_with_approver(self):
        bus, _ = build([(ADMIN, "approve")], self.st); self.assertEqual(bus.send("t", "remediate", INP, to="remediation").state, "completed")
        h = self.st.approvals_history()[0]
        self.assertEqual((h["status"], h["approver"], h["action"], h["ticket"], h["tier"]), ("executed", ADMIN, "restart_service", "INC1", "human"))
        self.assertTrue(h["decided_at"] and h["finished_at"])

    def test_rejection_and_timeout_are_recorded(self):
        bus, t = build([(ADMIN, "reject")], self.st); bus.send("t", "remediate", INP, to="remediation")
        bus2, _ = build([], self.st); bus2.send("t", "remediate", {**INP, "ticket": "INC2"}, to="remediation")
        st = {h["ticket"]: h["status"] for h in self.st.approvals_history()}
        self.assertEqual(st, {"INC1": "rejected", "INC2": "expired"}); self.assertEqual(t.executed, [])

    def test_failed_execution_is_recorded(self):
        bus, t = build([(ADMIN, "approve")], self.st)
        with patch.object(t, "act", side_effect=RuntimeError("ssm down")):
            self.assertEqual(bus.send("t", "remediate", INP, to="remediation").state, "failed")
        h = self.st.approvals_history()[0]; self.assertEqual(h["status"], "failed"); self.assertIn("ssm down", h["detail"])

    def test_second_attempt_denied_and_audited(self):
        bus, _ = build([(ADMIN, "approve")] * 2, self.st); bus.send("t", "remediate", INP, to="remediation")
        self.assertEqual(bus.send("t", "remediate", INP, to="remediation").state, "failed")
        self.assertEqual([h["status"] for h in self.st.approvals_history()], ["denied", "executed"])

    def test_history_endpoint(self):
        from fury.webapp import Ctx, create_app
        t, s, c = FakeTarget(), FakeSnow(), FakeChat([(ADMIN, "approve")])
        bus, triage, _ = system.build(target=t, snow=s, chat=c, store=self.st)
        client = create_app(Ctx(bus, triage, s, c, "i-1", t, store=self.st), demo=True).test_client()
        bus.send("t", "remediate", INP, to="remediation")
        d = client.get("/api/approvals/history").get_json(); self.assertTrue(d["enabled"]); self.assertEqual(d["history"][0]["status"], "executed")

if __name__ == "__main__": unittest.main()
