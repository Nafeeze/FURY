import sys, unittest, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import aws
from fury.slack_chat import SlackChat

class FakeSsm:
    """Mimics boto3 ssm client: records calls, returns scripted invocation output."""
    class exceptions: pass
    def __init__(self, outputs): self.outputs, self.sent = list(outputs), []; self.polls = 0
    def send_command(self, **kw): self.sent.append(kw); return {"Command": {"CommandId": "c1"}}
    def get_command_invocation(self, **kw):
        self.polls += 1
        if self.polls == 1: raise type("InvocationDoesNotExist", (Exception,), {})("not yet")
        o = self.outputs[0]; return o
class FakeSession:
    def __init__(self, ssm): self.ssm = ssm
    def client(self, name, region_name=None): return self.ssm

class AwsTests(unittest.TestCase):
    def test_read_target_builds_right_command_and_parses(self):
        ssm = FakeSsm([{"Status": "Success", "StandardOutputContent": 'noise\n{"drive":"F:","used_percent":94.1,"tmp_mb":1500,"healthy":false}\n'}])
        t = aws.AwsReadTarget(FakeSession(ssm), "ap-south-2", "i-1"); t.r.poll = 0
        r = t.check_disk("F:")
        self.assertEqual(r["used_percent"], 94.1); self.assertFalse(r["healthy"])
        self.assertEqual(ssm.sent[0]["DocumentName"], "Fury-Check-Disk"); self.assertEqual(ssm.sent[0]["Parameters"], {"Drive": ["F:"]})
        self.assertEqual(ssm.sent[0]["InstanceIds"], ["i-1"])
    def test_act_target_uses_act_documents_only(self):
        ssm = FakeSsm([{"Status": "Success", "StandardOutputContent": '{"restarted":"W3SVC","state":"running"}'}])
        t = aws.AwsActTarget(FakeSession(ssm), "ap-south-2", "i-1"); t.r.poll = 0
        t.act("restart_service", "W3SVC"); self.assertEqual(ssm.sent[0]["DocumentName"], "Fury-Act-RestartService"); self.assertEqual(ssm.sent[0]["Parameters"], {"Name": ["W3SVC"]})
        with self.assertRaises(ValueError): t.act("format_disk", "C:")
        self.assertFalse(hasattr(aws.AwsReadTarget, "act"))
    def test_failed_command_raises(self):
        ssm = FakeSsm([{"Status": "Failed", "StandardErrorContent": "boom"}]); t = aws.AwsReadTarget(FakeSession(ssm), "r", "i-1"); t.r.poll = 0
        with self.assertRaises(aws.SsmError): t.check_cpu()
    def test_alarm_poller_handles_each_state_change_once(self):
        a = {"AlarmName": "Fury-CPU-High", "AlarmDescription": "CPU utilization above 90%", "MetricName": "CpuPercent", "StateUpdatedTimestamp": "t1"}
        class CW:
            def __init__(s): s.alarms = [a]
            def describe_alarms(s, **kw): return {"MetricAlarms": s.alarms}
        got = []; cw = CW(); p = aws.AlarmPoller(cw, "i-1", got.append)
        self.assertEqual(p.poll_once(), 1); self.assertEqual(p.poll_once(), 0)
        self.assertEqual(got[0]["metric"], "cpu"); self.assertEqual(got[0]["instance"], "i-1")
        cw.alarms = [{**a, "StateUpdatedTimestamp": "t2"}]; self.assertEqual(p.poll_once(), 1)

class SlackLogic(unittest.TestCase):
    def test_button_and_typed_replies(self):
        c = SlackChat("x", "y"); c._on_button("U1", "r1", "approve"); self.assertEqual(c.wait_decision("r1", 0.1), ("U1", "approve"))
        c.ts2req["111.1"] = "r2"
        c._on_message({"user": "U2", "text": "Yes.", "thread_ts": "111.1"}); self.assertEqual(c.wait_decision("r2", 0.1), ("U2", "approve"))
        c._on_message({"user": "U2", "text": "no", "thread_ts": "111.1"}); self.assertEqual(c.wait_decision("r2", 0.1), ("U2", "reject"))
    def test_ignores_bots_unrelated_and_chatter(self):
        c = SlackChat("x", "y"); c.ts2req["9.9"] = "r3"
        c._on_message({"user": "U", "text": "yes", "thread_ts": "9.9", "bot_id": "B1"})
        c._on_message({"user": "U", "text": "yes", "thread_ts": "other"})
        c._on_message({"user": "U", "text": "maybe later", "thread_ts": "9.9"})
        self.assertIsNone(c.wait_decision("r3", 0.1))
    def test_timeout_returns_none_and_pending_tracking(self):
        c = SlackChat("x", "y"); self.assertIsNone(c.wait_decision("none", 0.1))
        c._pending["r"] = {"text": "t", "since": "s"}; self.assertEqual(len(c.pending()), 1); c.close("r"); self.assertEqual(c.pending(), [])
    def test_unauthorised_slack_user_goes_through_comms_check(self):
        from fury import system
        from fury.adapters import FakeTarget, FakeSnow
        c = SlackChat("x", "y"); c.post = lambda *a, **k: 1; c.post_approval = lambda *a, **k: 1
        c._on_button("U_STRANGER", "INC0010001:restart_service", "approve")
        t = FakeTarget(); t.inject("service_down")
        bus, _, _ = system.build(target=t, snow=FakeSnow(), chat=c, ttl=1)
        out = system.run_alert(bus, system.ALERTS["service_down"]).output
        self.assertEqual(out["outcome"], "escalated"); self.assertEqual(t.executed, [])

if __name__ == "__main__": unittest.main(verbosity=2)
