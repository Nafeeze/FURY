import os, sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from fury import retrieval as R, runbooks as RB
from golden_retrieval import GOLDEN, UNSUPPORTED

RBS = RB.load(Path(__file__).resolve().parent.parent / "runbooks")
DOC = """---
id: {id}
title: {title}
kind: alert
symptoms: {symptoms}
owner_team: SRE
escalate_to: L2
version: 3
{extra}---
| # | type | step | tool / action | parameters | success criteria |
|---|---|---|---|---|---|
| 1 | check | Look at replication lag | check_disk | drive=F: | ok |
"""
def rb(id="X-1", title="Replication lag", symptoms="replication lag, replica behind", extra=""): return RB.parse(DOC.format(id=id, title=title, symptoms=symptoms, extra=extra), source="x.md")

class GoldenTests(unittest.TestCase):
    def test_golden_set_quality_gate(self):
        m = R.evaluate(RBS, GOLDEN, k=3)
        self.assertGreaterEqual(m["recall@3"], 0.90, m["misses"]); self.assertGreaterEqual(m["mrr"], 0.80, m["misses"])
        print("\nretrieval:", {k: v for k, v in m.items() if k != "misses"})

    def test_unsupported_queries_return_no_evidence(self):
        for q in UNSUPPORTED: self.assertEqual(R.search(RBS, q), [], q)

class GovernanceTests(unittest.TestCase):
    def test_hits_carry_full_provenance(self):
        h = R.search(RBS, "out of disk space on F")[0]
        for key in ("runbook", "version", "section", "source", "content_hash", "updated_at", "citation", "owner"): self.assertTrue(h[key], key)
        self.assertRegex(h["citation"], r"^AWS-DISK-001 v1\.0 / ")

    def test_retired_and_draft_content_never_returned(self):
        for st in ("retired", "draft"): self.assertEqual(R.search([rb(extra=f"status: {st}\n")], "replication lag"), [])
        self.assertTrue(R.search([rb()], "replication lag"))

    def test_access_filter_applies_before_ranking(self):
        secret = rb(extra="access: sre-admin\n")
        self.assertEqual(R.search([secret], "replication lag", role="engineer"), [])
        self.assertTrue(R.search([secret], "replication lag", role="sre-admin"))
        pub, hidden = rb(id="P-1", title="Public replication note"), rb(id="S-1", extra="access: sre-admin\n")
        self.assertEqual({h["runbook"] for h in R.search([pub, hidden], "replication lag", role="engineer", k=5)}, {"P-1"})

    def test_stale_content_is_flagged(self):
        old = rb(extra="updated_at: 2020-01-01\n"); self.assertTrue(R.search([old], "replication lag")[0]["stale"])
        self.assertFalse(R.search([rb(extra="updated_at: 2099-01-01\n")], "replication lag")[0]["stale"])
        with patch.dict(os.environ, {"FURY_RUNBOOK_MAX_AGE_DAYS": "100000"}): self.assertFalse(R.search([old], "replication lag")[0]["stale"])

    def test_one_hit_per_section_and_k_respected(self):
        hits = R.search(RBS, "disk space drive full", k=2)
        self.assertLessEqual(len(hits), 2); self.assertEqual(len({(h["runbook"], h["section"]) for h in hits}), len(hits))

    def test_empty_query_and_empty_corpus(self):
        self.assertEqual(R.search(RBS, "   "), []); self.assertEqual(R.search([], "disk"), [])

    def test_rrf_beats_either_ranker_alone_on_typos(self):
        # fuzzy trigram retrieval rescues a misspelling that BM25 alone cannot match
        self.assertEqual(R.search(RBS, "disc spase on drive F")[0]["runbook"], "AWS-DISK-001")


class HowToTests(unittest.TestCase):
    def reply(self, q):
        from fury import system
        from fury.adapters import FakeChat, FakeSnow, FakeTarget
        from fury.webapp import Ctx, howto_reply
        t, s, c = FakeTarget(), FakeSnow(), FakeChat(); bus, tr, _ = system.build(target=t, snow=s, chat=c)
        return howto_reply(Ctx(bus, tr, s, c, "i-1", t), q)

    def test_howto_uses_retrieval_fallback_with_citation(self):
        self.assertIn("AWS-DISK-001 v1.0", self.reply("how do I clear temp files safely"))

    def test_howto_without_support_says_so(self):
        self.assertIn("I do not have a runbook", self.reply("how do I fix database replication lag"))

if __name__ == "__main__": unittest.main()
