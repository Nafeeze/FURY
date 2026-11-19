import unittest
from fury.slo_alert import SloWatcher


def snap(**status):
    return lambda: [{"id": k, "description": f"{k} desc", "objective": ">= 1", "measured": 0.5, "unit": "", "status": v} for k, v in status.items()]


class SloAlertTests(unittest.TestCase):
    def watcher(self, seq, notify=None):
        out, it = [], iter(seq)
        w = SloWatcher(notify or out.append, evaluate=lambda: next(it)(), interval=0)
        return w, out

    def test_breach_alerts_once_then_recovery_once(self):
        w, out = self.watcher([snap(a="ok"), snap(a="breach"), snap(a="breach"), snap(a="ok"), snap(a="ok")])
        for _ in range(5): w.check_once()
        self.assertEqual(len(out), 2)
        self.assertIn("breach", out[0]); self.assertIn("recovered", out[1])

    def test_no_data_is_never_an_alert(self):
        w, out = self.watcher([snap(a="no_data"), snap(a="no_data")])
        w.check_once(); w.check_once()
        self.assertEqual(out, [])

    def test_first_observation_already_in_breach_alerts(self):
        w, out = self.watcher([snap(a="breach")])
        w.check_once()
        self.assertEqual(len(out), 1)

    def test_a_failing_notifier_never_raises(self):
        def boom(_): raise RuntimeError("slack down")
        w, out = self.watcher([snap(a="breach")], notify=boom)
        self.assertEqual(len(w.check_once()), 1)   # recorded as sent attempt, exception swallowed

    def test_breach_is_audited_without_secrets(self):
        from fury.audit import Audit
        a = Audit(); out = []
        w = SloWatcher(out.append, evaluate=snap(a="breach"), audit=a); w.check_once()
        self.assertEqual(a.entries[-1]["action"], "slo.alert"); self.assertEqual(a.entries[-1]["outcome"], "breach")


if __name__ == "__main__": unittest.main()
