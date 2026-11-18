import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import runbooks as RB, system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.webapp import Ctx, create_app

DOC = """---
id: T-001
title: Test runbook
kind: alert
symptoms: disk full, no space left
owner_team: SRE-Platform
escalate_to: SRE-Platform L2
{extra}---
# T-001

| # | type | step | tool / action | parameters | success criteria |
|---|---|---|---|---|---|
| 1 | check | Confirm | check_disk | drive=F: | ok |
"""

class ProvenanceTests(unittest.TestCase):
    def test_metadata_parsed_and_citation(self):
        rb = RB.parse(DOC.format(extra="version: 2.1\nstatus: active\nupdated_at: 2026-11-01\n"), source="t.md")
        self.assertEqual((rb.version, rb.status, rb.updated_at, rb.source), ("2.1", "active", "2026-11-01", "t.md"))
        self.assertEqual(rb.citation(), "T-001 v2.1 (updated 2026-11-01)")
        self.assertEqual(len(rb.content_hash), 12); self.assertTrue(rb.indexed_at)
        self.assertEqual(rb.as_dict()["citation"], rb.citation())

    def test_legacy_runbook_without_metadata_still_loads(self):
        rb = RB.parse(DOC.format(extra=""))
        self.assertEqual((rb.version, rb.status), ("1", "active")); self.assertEqual(rb.citation(), "T-001 v1")

    def test_hash_changes_when_content_changes(self):
        a = RB.parse(DOC.format(extra="")); b = RB.parse(DOC.format(extra="version: 2\n"))
        self.assertNotEqual(a.content_hash, b.content_hash)

    def test_retired_and_draft_runbooks_are_never_matched(self):
        for st in ("retired", "draft"):
            rb = RB.parse(DOC.format(extra=f"status: {st}\n"))
            self.assertIsNone(RB.match([rb], "disk full no space left")[0])
        self.assertIsNotNone(RB.match([RB.parse(DOC.format(extra=""))], "disk full no space left")[0])

    def test_real_runbooks_all_carry_version_and_status(self):
        rbs = RB.load(Path(__file__).resolve().parent.parent / "runbooks")
        self.assertTrue(rbs)
        for rb in rbs: self.assertTrue(rb.version and rb.status == "active" and rb.updated_at and rb.source.endswith(".md"), rb.id)

    def test_ticket_work_note_cites_runbook_version(self):
        t, snow, chat = FakeTarget(), FakeSnow(), FakeChat([("U0CAZH32V2T", "approve")]); t.inject("disk_full")
        bus, triage, _ = system.build(target=t, snow=snow, chat=chat)
        out = bus.send("cw", "handle_alert", system.ALERTS["disk_full"], to="triage").output
        self.assertEqual(out["outcome"], "resolved")
        notes = " ".join(n["text"] for n in snow.get_notes(out["ticket"]))
        self.assertIn("Runbook: AWS-DISK-001 v1.0 (updated 2026-11-18)", notes)

    def test_howto_reply_shows_source(self):
        t, snow, chat = FakeTarget(), FakeSnow(), FakeChat()
        bus, triage, _ = system.build(target=t, snow=snow, chat=chat)
        from fury.webapp import howto_reply
        msg = howto_reply(Ctx(bus, triage, snow, chat, "i-1", t), "how do I fix a full disk")
        self.assertIn("AWS-DISK-001 v1.0", msg); self.assertIn("aws-disk-001_data_drive_full.md", msg)

if __name__ == "__main__": unittest.main()
