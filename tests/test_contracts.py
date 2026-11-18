import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import contracts, system
from fury.adapters import FakeChat, FakeSnow, FakeTarget

class ContractTests(unittest.TestCase):
    def test_every_skill_in_the_system_has_a_contract(self):
        bus, _, _ = system.build(target=FakeTarget())
        offered = {s for a in bus.agents.values() for s in a.card()["skills"]}
        self.assertEqual(offered - set(contracts.CONTRACTS), set(), "a skill without a contract bypasses validation")
        self.assertEqual(set(contracts.CONTRACTS) - offered, set(), "contract for a skill nobody offers")

    def test_validate_rules(self):
        v = contracts.validate
        self.assertIsNone(v("match_runbook", {"query": "disk"}))
        self.assertIn("missing", v("match_runbook", {}))
        self.assertIn("must be str", v("match_runbook", {"query": 5}))
        self.assertIn("must be list", v("run_checks", {"steps": "check_disk"}))
        self.assertIn("string 'tool'", v("run_checks", {"steps": [{"params": {}}]}))
        self.assertIn("params", v("run_checks", {"steps": [{"tool": "check_disk", "params": "x"}]}))
        self.assertIn("object", v("add_note", "oops")); self.assertIsNone(v("unknown_skill", {}))

    def test_bus_rejects_invalid_input_before_the_agent_runs(self):
        t = FakeTarget(); bus, _, _ = system.build(target=t, chat=FakeChat([("U0CAZH32V2T", "approve")]))
        r = bus.send("x", "remediate", {"action": "restart_service", "target": "W3SVC"}, to="remediation")   # no ticket/summary
        self.assertEqual(r.state, "failed"); self.assertIn("invalid input for remediate", r.error); self.assertEqual(t.executed, [])
        r = bus.send("x", "run_checks", {"steps": [{"tool": ["check_disk"]}]}, to="diagnostic")
        self.assertEqual(r.state, "failed"); self.assertIn("invalid input", r.error)

    def test_contract_violations_are_counted(self):
        from fury import telemetry
        bus, _, _ = system.build(target=FakeTarget()); bus.send("x", "match_runbook", {}, to="knowledge")
        self.assertIn("contract.violation", str(telemetry.summary()))

    def test_normal_flow_still_passes_validation(self):
        t = FakeTarget(); t.inject("disk_full"); bus, _, _ = system.build(target=t, snow=FakeSnow(), chat=FakeChat([("U0CAZH32V2T", "approve")]))
        self.assertEqual(bus.send("cw", "handle_alert", system.ALERTS["disk_full"], to="triage").output["outcome"], "resolved")

class EvidenceTests(unittest.TestCase):
    def test_evidence_is_structured_and_timestamped(self):
        bus, _, _ = system.build(target=FakeTarget())
        ev = bus.send("x", "run_checks", {"steps": [{"tool": "check_disk", "params": {"drive": "F:"}}, {"tool": "check_service", "params": {"name": "W3SVC"}}]}, to="diagnostic").output["evidence"]
        self.assertEqual([e["evidence_id"] for e in ev], ["E1", "E2"])
        for e in ev:
            self.assertEqual(e["mode"], "read"); self.assertRegex(e["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
            self.assertEqual(e["healthy"], e["result"].get("healthy")); self.assertTrue(e["tool"] and "params" in e)

    def test_read_only_agent_still_refuses_write_tools(self):
        bus, _, _ = system.build(target=FakeTarget())
        r = bus.send("x", "run_checks", {"steps": [{"tool": "act", "params": {}}]}, to="diagnostic")
        self.assertEqual(r.state, "failed"); self.assertIn("read-only", r.error)

if __name__ == "__main__": unittest.main()
