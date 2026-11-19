import os, unittest
from unittest.mock import patch
from fury import kb as KB, retrieval, runbooks as RB, vectors, webapp
from fury.adapters import FakeSnow, FakeChat, FakeTarget
from fury import system

ARTICLES = [
    {"number": "KB0010001", "short_description": "Recovering disk space safely", "workflow_state": "published", "version": "2", "sys_updated_on": "2026-11-01 10:00:00",
     "text": "<p>Clear temp and cache folders once nothing is writing to them.</p><p>Never delete audit logs.</p>", "kb_knowledge_base": {"display_value": "IT"}},
    {"number": "KB0010002", "short_description": "Draft idea about printers", "workflow_state": "draft", "text": "<p>printer printer printer</p>"},
    {"number": "KB0010003", "short_description": "Retired article about floppies", "workflow_state": "retired", "text": "<p>floppy disk guidance</p>"},
    {"number": "KB0010004", "short_description": "IIS restart guidance", "workflow_state": "published", "text": "<script>evil()</script><p>Restart W3SVC only when stopped &amp; unresponsive.</p>"},
]


class IngestTests(unittest.TestCase):
    def test_only_published_articles_are_ingested_and_markup_is_stripped(self):
        docs = KB.to_docs(ARTICLES)
        self.assertEqual([d.id for d in docs], ["KB0010001", "KB0010004"])
        body = " ".join([docs[1].symptoms] + [s.text for s in docs[1].steps])
        self.assertNotIn("evil", body); self.assertNotIn("<", body); self.assertIn("stopped & unresponsive", body)
        self.assertEqual(docs[0].version, "2"); self.assertEqual(docs[0].owner_team, "IT"); self.assertEqual(docs[0].kind, "kb")

    def test_refresh_keeps_previous_index_when_servicenow_fails(self):
        snow = FakeSnow(); snow.kb_articles = ARTICLES
        k = KB.KnowledgeBase(snow); self.assertEqual(k.refresh(), 2)
        snow.fail = True; snow.list_kb = lambda limit=200: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertEqual(k.refresh(), 2); self.assertEqual(k.last_error, "RuntimeError")   # stale but available, and reported

    def test_kb_articles_are_searchable_with_citations(self):
        hits = retrieval.search(KB.to_docs(ARTICLES), "how do I recover disk space", k=2)
        self.assertTrue(hits); self.assertEqual(hits[0]["runbook"], "KB0010001"); self.assertIn("KB0010001", hits[0]["citation"])

    def test_unsupported_question_returns_nothing_from_kb(self):
        self.assertEqual(retrieval.search(KB.to_docs(ARTICLES), "how to bake a cake"), [])

    def test_kb_docs_never_reach_the_agents_that_act(self):
        bus, tri, env = system.build(target=FakeTarget(), snow=FakeSnow(), chat=FakeChat(), approvers=["U1"])
        ids = {r.id for r in bus.agents["knowledge"].rbs}
        self.assertFalse(ids & {"KB0010001", "KB0010004"})


class FakeVectors:
    """Deterministic 'embeddings': two topics on separate axes, so a paraphrase with no shared words still matches."""
    def __init__(self, floor_hit=0.9, miss=0.1): self.hit, self.miss = floor_hit, miss
    def scores(self, query, texts):
        sluggish = any(w in query.lower() for w in ("sluggish", "crawling"))
        return [self.hit if (sluggish and ("cpu" in t.lower())) else self.miss for t in texts]


class SemanticRankerTests(unittest.TestCase):
    def setUp(self): self.rbs = RB.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runbooks"))

    def test_paraphrase_with_no_shared_words_is_found_only_with_the_semantic_ranker(self):
        q = "the box feels sluggish and crawling"
        self.assertEqual(retrieval.search(self.rbs, q), [])
        hits = retrieval.search(self.rbs, q, vectors=FakeVectors())
        self.assertTrue(hits); self.assertEqual(hits[0]["runbook"], "AWS-CPU-001")

    def test_semantic_floor_still_lets_the_system_abstain(self):
        self.assertEqual(retrieval.search(self.rbs, "how to bake a cake", vectors=FakeVectors(miss=0.3)), [])

    def test_unavailable_embeddings_fall_back_to_lexical_search(self):
        class Down:
            def scores(self, q, t): return None
        self.assertEqual([h["runbook"] for h in retrieval.search(self.rbs, "high cpu utilization", vectors=Down())][:1], ["AWS-CPU-001"])

    def test_vector_index_caches_passages_and_survives_outage(self):
        calls = []
        def embed(texts, kind): calls.append((kind, len(texts))); return [[1.0, 0.0] if "cpu" in t else [0.0, 1.0] for t in texts]
        vi = vectors.VectorIndex(embed=embed, cache_path="")
        self.assertEqual(vi.scores("cpu", ["cpu a", "disk b"]), [1.0, 0.0])
        vi.scores("cpu again", ["cpu a", "disk b"])
        self.assertEqual([c for c in calls if c[0] == "passage"], [("passage", 2)], "passages must be embedded once, then cached")
        self.assertIsNone(vectors.VectorIndex(embed=lambda t, k: None, cache_path="").scores("x", ["a"]))


class ChatKbTests(unittest.TestCase):
    def ctx(self):
        snow = FakeSnow(); snow.kb_articles = ARTICLES
        bus, tri, env = system.build(target=FakeTarget(), snow=snow, chat=FakeChat(), approvers=["U1"])
        c = webapp.Ctx(bus, tri, snow, FakeChat(), "i-1", FakeTarget()); c.kb.refresh(); return c

    def test_question_without_runbook_is_answered_from_kb_with_citation_and_disclaimer(self):
        out = webapp.kb_answer(self.ctx(), "how do I recover disk space safely")
        self.assertIn("KB0010001", out); self.assertIn("informational", out.lower())

    def test_no_supporting_article_means_no_kb_answer(self):
        self.assertIsNone(webapp.kb_answer(self.ctx(), "how to bake a cake"))


if __name__ == "__main__": unittest.main()
