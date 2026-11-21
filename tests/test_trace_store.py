"""Durable traces: spans survive a restart, are already redacted, and a broken store never breaks the app."""
import os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import telemetry


class DurableTraces(unittest.TestCase):
    def setUp(self): self.addCleanup(self._off)
    def _off(self): telemetry._sink = None; telemetry.reset()

    def test_spans_survive_a_restart_and_stay_redacted(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        telemetry.reset(); telemetry.persist(path)
        with telemetry.span("run", source="chat", label="mail me at someone@example.com") as sp: tid = sp["trace_id"]
        with telemetry.span("child"): pass
        telemetry._sink.flush()
        telemetry._sink = None; telemetry.reset()                    # "restart": memory gone, file stays
        self.assertEqual(telemetry.persist(path), 2)
        t = telemetry.trace(tid)
        self.assertIsNotNone(t); self.assertEqual(t["spans"][0]["name"], "run")
        self.assertNotIn("someone@example.com", str(t))

    def test_retention_drops_old_spans(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        telemetry.persist(path)
        telemetry._sink._db.execute("INSERT INTO spans VALUES ('t','s',?,'{}')", (time.time() - 90 * 86400,)); telemetry._sink._db.commit()
        telemetry._sink = None; telemetry.reset()
        self.assertEqual(telemetry.persist(path, retention_days=30), 0)

    def test_a_failing_store_never_raises_into_the_app(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        telemetry.persist(path)
        telemetry._sink._q.put({"trace_id": "x"})                    # malformed record: the writer must absorb it
        with telemetry.span("still-works"): pass
        telemetry._sink.flush(); self.assertTrue(telemetry._sink.last_error)

    def test_retention_text_reflects_the_mode(self):
        self.assertIn("memory only", telemetry.retention())
        telemetry.persist(os.path.join(tempfile.mkdtemp(), "t.db")); self.assertIn("stored in t.db", telemetry.retention())


if __name__ == "__main__": unittest.main()
