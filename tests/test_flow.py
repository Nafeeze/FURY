import sys, unittest, os, json, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system, approvals, a2a
from fury.adapters import FakeTarget, FakeSnow, FakeChat, SnowClient
from fury.system import ALERTS

ADMIN = "U0CAZH32V2T"

def run(fault, chat_script=None, fix_works=True, snow=None, ttl=600):
    t = FakeTarget(fix_works=fix_works); t.inject(fault)
    s = snow or FakeSnow(); c = FakeChat(chat_script)
    bus, triage, env = system.build(target=t, snow=s, chat=c, ttl=ttl)
    task = system.run_alert(bus, ALERTS[fault])
    assert task.state == "completed", task.error
    return task.output, env, bus

def notes(env, n): return "\n".join(env["snow"].incidents[n]["work_notes"])

class HappyPaths(unittest.TestCase):
    def test_each_runbook_resolves_after_approval(self):
        for fault, action in [("disk_full", "clear_temp_files"), ("service_down", "restart_service"), ("cpu_runaway", "kill_process")]:
            out, env, _ = run(fault, [(ADMIN, "approve")])
            self.assertEqual(out["outcome"], "resolved", (fault, out))
            inc = env["snow"].incidents[out["ticket"]]
            self.assertEqual(inc["state"], "6"); self.assertIn("Solved", inc["close_code"])
            self.assertEqual(env["target"].executed[0][0], action)
            self.assertIn(ADMIN, inc["close_notes"]); self.assertIn("Before:", inc["close_notes"]); self.assertIn("After:", inc["close_notes"])
            self.assertIn("Approved by", notes(env, out["ticket"])); self.assertIn("Verification", notes(env, out["ticket"]))

    def test_no_runbook_escalates_without_touching_server(self):
        out, env, _ = run("memory_pressure")
        self.assertEqual(out["outcome"], "escalated"); self.assertIn("no runbook", out["reason"])
        self.assertEqual(env["target"].executed, [])
        inc = env["snow"].incidents[out["ticket"]]
        self.assertEqual(inc["state"], "2"); self.assertEqual(inc["assignment_group"], "SRE-Platform L2")
        self.assertTrue(any("Escalation" in m[1] for m in env["chat"].messages))

class SafetyAndFailures(unittest.TestCase):
    def test_rejection_means_no_action_and_escalation(self):
        out, env, _ = run("service_down", [(ADMIN, "reject")])
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(env["target"].executed, [])
        self.assertIn("rejected", notes(env, out["ticket"]))
    def test_unauthorised_approver_is_ignored(self):
        out, env, _ = run("disk_full", [("U_RANDOM", "approve")])
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(env["target"].executed, [])
        self.assertTrue(any("not an authorised approver" in m[1] for m in env["chat"].messages))
    def test_timeout_means_escalate(self):
        out, env, _ = run("cpu_runaway", [])
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(env["target"].executed, [])
    def test_failed_fix_is_never_marked_resolved(self):
        out, env, _ = run("disk_full", [(ADMIN, "approve")], fix_works=False)
        self.assertEqual(out["outcome"], "escalated"); self.assertIn("verification failed", out["reason"])
        inc = env["snow"].incidents[out["ticket"]]
        self.assertNotEqual(inc["state"], "6"); self.assertIn("ESCALATED", notes(env, out["ticket"]))
    def test_false_alarm_creates_no_ticket(self):
        t = FakeTarget(); s = FakeSnow(); bus, _, env = system.build(target=t, snow=s)
        out = system.run_alert(bus, ALERTS["disk_full"]).output
        self.assertEqual(out["outcome"], "false_alarm"); self.assertEqual(s.incidents, {})
    def test_duplicate_alarm_reuses_ticket(self):
        t = FakeTarget(); t.inject("memory_pressure"); s = FakeSnow(); bus, _, _ = system.build(target=t, snow=s)
        a = system.run_alert(bus, ALERTS["memory_pressure"]).output; b = system.run_alert(bus, ALERTS["memory_pressure"]).output
        self.assertEqual(b["outcome"], "duplicate"); self.assertEqual(a["ticket"], b["ticket"]); self.assertEqual(len(s.incidents), 1)
    def test_snow_down_means_no_action(self):
        out, env, _ = run("service_down", [(ADMIN, "approve")], snow=FakeSnow(fail=True))
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(env["target"].executed, [])
        self.assertTrue(any("no ticket" in m[1] or "(no ticket)" in m[1] for m in env["chat"].messages))
    def test_disk_not_in_tmp_escalates(self):
        t = FakeTarget(); t.inject("disk_full"); t.disk["tmp_mb"] = 100
        bus, _, env = system.build(target=t, chat=FakeChat([(ADMIN, "approve")]))
        out = system.run_alert(bus, ALERTS["disk_full"]).output
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(t.executed, [])
    def test_cpu_culprit_not_allowlisted_escalates(self):
        t = FakeTarget(); t.procs["sqlservr"] = 99
        bus, _, env = system.build(target=t, chat=FakeChat([(ADMIN, "approve")]))
        out = system.run_alert(bus, ALERTS["cpu_runaway"]).output
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(t.executed, [])

class LeastPrivilege(unittest.TestCase):
    def test_diagnostic_cannot_act(self):
        bus, _, _ = system.build()
        t = bus.send("test", "run_checks", {"steps": [{"tool": "act", "params": {}}]})
        self.assertEqual(t.state, "failed"); self.assertIn("read-only", t.error)
    def test_readonly_view_has_no_act(self):
        with self.assertRaises(AttributeError): system.ReadOnlyView(FakeTarget()).act("x", "y")
    def test_remediation_refuses_non_catalog_action(self):
        bus, _, _ = system.build(chat=FakeChat([(ADMIN, "approve")]))
        t = bus.send("test", "remediate", {"action": "restart_service", "target": "sshd", "ticket": "INC1", "summary": "x"})
        self.assertEqual(t.state, "failed"); self.assertIn("not in the approved catalog", t.error)
    def test_forged_and_mismatched_tokens_fail(self):
        tok = approvals.sign("restart_service", "W3SVC", "INC1", ADMIN)
        self.assertIsNotNone(approvals.verify(tok, "restart_service", "W3SVC", "INC1")[0])
        self.assertIsNone(approvals.verify(tok, "kill_process", "fury-rogue", "INC1")[0])
        self.assertIsNone(approvals.verify(tok, "restart_service", "W3SVC", "INC2")[0])
        self.assertIsNone(approvals.verify(tok[:-3] + "abc", "restart_service", "W3SVC", "INC1")[0])
        self.assertEqual(approvals.verify(approvals.sign("a", "b", "c", "u", ttl=-1), "a", "b", "c")[1], "approval expired")
    def test_restart_only_once_per_incident(self):
        bus, _, env = system.build(chat=FakeChat([(ADMIN, "approve"), (ADMIN, "approve")]))
        r = {"action": "restart_service", "target": "W3SVC", "ticket": "INC9", "summary": "x"}
        self.assertEqual(bus.send("t", "remediate", r).state, "completed")
        self.assertEqual(bus.send("t", "remediate", r).state, "failed")

class A2AAndSnow(unittest.TestCase):
    def test_discovery_by_skill_and_cards(self):
        bus, _, _ = system.build()
        self.assertEqual(bus.find("run_checks"), "diagnostic"); self.assertEqual(bus.find("resolve"), "itsm")
        self.assertIn("remediate", bus.agents["remediation"].card()["skills"])
    def test_http_transport_roundtrip(self):
        from werkzeug.serving import make_server
        bus, _, _ = system.build(); app = a2a.serve(bus.agents["knowledge"], 0)
        srv = make_server("127.0.0.1", 0, app); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ra = a2a.RemoteAgent(f"http://127.0.0.1:{srv.server_port}")
            t = a2a.Task(skill="match_runbook", input={"query": "W3SVC service stopped"}); ra.run(t)
            self.assertEqual(t.state, "completed"); self.assertEqual(t.output["runbook"]["id"], "AWS-SVC-001")
        finally: srv.shutdown()
    def test_snow_client_requests(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []
        class H(BaseHTTPRequestHandler):
            def _do(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                out = {"result": {"number": "INC0010009", "sys_id": "abc"}} if self.command == "POST" else {"result": [{"number": "INC0010009", "sys_id": "abc"}]}
                if self.command == "PATCH": out = {"result": {}}
                b = json.dumps(out).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            do_GET = do_POST = do_PATCH = _do
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = SnowClient(f"http://127.0.0.1:{srv.server_port}", "u", "p")
            n = c.create({"short_description": "x"}); c.add_note(n, "hello"); c.update(n, {"state": "6", "close_code": "Solved (Permanently)", "close_notes": "n"})
            self.assertEqual(n, "INC0010009")
            self.assertEqual(seen[0][0:2], ("POST", "/api/now/table/incident")); self.assertTrue(seen[0][2].startswith("Basic "))
            self.assertEqual(seen[1][0:2], ("PATCH", "/api/now/table/incident/abc")); self.assertEqual(json.loads(seen[1][3]), {"work_notes": "hello"})
            self.assertEqual(json.loads(seen[2][3])["state"], "6")
        finally: srv.shutdown()

class RecurrenceTests(unittest.TestCase):
    def test_real_client_never_treats_resolved_closed_canceled_as_open(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import urllib.parse
        seen = []
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(urllib.parse.unquote_plus(self.path)); b = b'{"result": []}'
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertIsNone(SnowClient(f"http://127.0.0.1:{srv.server_port}", "u", "p").find_open("Fury-Disk-F-High i-1"))
            self.assertIn("stateNOT IN6,7,8", seen[0]); self.assertIn("active=true", seen[0])
        finally: srv.shutdown()
    def test_recurrence_after_resolution_opens_a_new_ticket(self):
        from fury import system
        snow, tgt = FakeSnow(), FakeTarget()
        bus, triage, env = system.build(target=tgt, snow=snow, chat=FakeChat([("U1", "approve"), ("U1", "approve")]), approvers=["U1"])
        tgt.inject("disk_full"); first = system.run_alert(bus, system.ALERTS["disk_full"]).output
        self.assertEqual(first["outcome"], "resolved")
        tgt.inject("disk_full"); second = system.run_alert(bus, system.ALERTS["disk_full"]).output
        self.assertNotEqual(second["outcome"], "duplicate"); self.assertNotEqual(second["ticket"], first["ticket"])

class OwnershipTests(unittest.TestCase):
    def test_fury_owns_the_ticket_while_working_and_hands_over_on_escalation(self):
        from fury import system
        snow, tgt = FakeSnow(), FakeTarget()
        bus, triage, env = system.build(target=tgt, snow=snow, chat=FakeChat([("U1", "approve")]), approvers=["U1"])
        tgt.inject("disk_full"); out = system.run_alert(bus, system.ALERTS["disk_full"]).output
        inc = snow.incidents[out["ticket"]]
        self.assertEqual(inc["assigned_to"], "fury_integration"); self.assertEqual(inc["state"], "6")
        tgt2, snow2 = FakeTarget(), FakeSnow()
        bus2, _, _ = system.build(target=tgt2, snow=snow2, chat=FakeChat([("U1", "reject")]), approvers=["U1"])
        tgt2.inject("cpu_runaway"); out2 = system.run_alert(bus2, system.ALERTS["cpu_runaway"]).output
        self.assertEqual(out2["outcome"], "escalated")
        inc2 = snow2.incidents[out2["ticket"]]
        self.assertEqual(inc2["assigned_to"], ""); self.assertEqual(inc2["assignment_group"], "SRE-Platform L2")

if __name__ == "__main__": unittest.main(verbosity=2)
