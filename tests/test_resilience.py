import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.resilience import IntegrationUnavailable, Resilient, SNOW_READS
from fury.webapp import Ctx, create_app


class Flaky:
    """Fails the first `n` calls of every method, then works. Counts calls per method."""
    def __init__(self, n):
        self.n, self.calls = n, {}

    def _go(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.calls[name] <= self.n:
            raise ConnectionError(f"{name} blip")
        return name

    def find_open(self, k): return self._go("find_open")      # read
    def create(self, f): return self._go("create")             # write
    attr = "plain"


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def wrap(inner, **kw):
    return Resilient(inner, "snow", SNOW_READS, sleep=lambda s: None, **kw)


class ResilienceTests(unittest.TestCase):
    def test_reads_are_retried_and_recover(self):
        f = Flaky(2)
        self.assertEqual(wrap(f).find_open("k"), "find_open"); self.assertEqual(f.calls["find_open"], 3)

    def test_writes_are_never_retried(self):
        f = Flaky(1); w = wrap(f)
        with self.assertRaises(ConnectionError): w.create({})
        self.assertEqual(f.calls["create"], 1, "a retried write could duplicate a ticket")

    def test_read_gives_up_after_retries(self):
        f = Flaky(99)
        with self.assertRaises(ConnectionError): wrap(f, retries=2).find_open("k")
        self.assertEqual(f.calls["find_open"], 3)

    def test_non_callable_attributes_pass_through(self):
        self.assertEqual(wrap(Flaky(0)).attr, "plain")

    def test_circuit_opens_fails_fast_then_recovers(self):
        f, clk = Flaky(99), Clock(); w = wrap(f, retries=0, threshold=3, cooldown=30, clock=clk)
        for _ in range(3):
            with self.assertRaises(ConnectionError): w.find_open("k")
        before = f.calls["find_open"]
        with self.assertRaises(IntegrationUnavailable): w.find_open("k")
        self.assertEqual(f.calls["find_open"], before, "an open circuit must not touch the dead system")
        self.assertEqual(w.health()["state"], "open")
        clk.t = 31; f.n = 0                       # cooldown passed and the system is back
        self.assertEqual(w.find_open("k"), "find_open"); self.assertEqual(w.health()["state"], "ok")

    def test_writes_also_fail_fast_when_open(self):
        f, clk = Flaky(99), Clock(); w = wrap(f, threshold=1, clock=clk)
        with self.assertRaises(ConnectionError): w.find_open("k")
        with self.assertRaises(IntegrationUnavailable): w.create({})
        self.assertNotIn("create", f.calls)

    def test_health_reports_last_error(self):
        w = wrap(Flaky(99), retries=0)
        with self.assertRaises(ConnectionError): w.find_open("k")
        h = w.health()
        self.assertEqual((h["state"], h["consecutive_failures"]), ("degraded", 1)); self.assertIn("ConnectionError", h["last_error"])


class WiredTests(unittest.TestCase):
    def test_workflow_escalates_when_servicenow_is_down(self):
        t = FakeTarget(); t.inject("disk_full"); snow = wrap(FakeSnow(fail=True), retries=1); chat = FakeChat([("U0CAZH32V2T", "approve")])
        bus, _, _ = system.build(target=t, snow=snow, chat=chat)
        o = bus.send("cw", "handle_alert", system.ALERTS["disk_full"], to="triage").output
        self.assertEqual(o["outcome"], "escalated")
        self.assertEqual(t.executed, [], "no action without a ticket")
        self.assertTrue(any("Escalation" in m[1] for m in chat.messages))

    def test_health_endpoint(self):
        t, c = FakeTarget(), FakeChat(); snow = wrap(FakeSnow())
        bus, tr, _ = system.build(target=t, snow=snow, chat=c)
        d = create_app(Ctx(bus, tr, snow, c, "i-1", t), demo=True).test_client().get("/api/health").get_json()
        self.assertEqual(d["status"], "ok"); self.assertEqual(d["integrations"][0]["name"], "snow"); self.assertEqual(d["integrations"][1]["state"], "unmonitored")


if __name__ == "__main__":
    unittest.main()
