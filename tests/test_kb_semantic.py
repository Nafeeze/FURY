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

    def test_display_value_objects_and_wiki_bodies_are_handled(self):
        rows = [{"number": "KB1", "short_description": "Wiki style article", "workflow_state": "Published", "text": None, "wiki": "<p>Body from the wiki field.</p>",
                 "version": {"display_value": "1.0", "link": "x"}, "kb_knowledge_base": {"display_value": "Known Error"}},
                {"number": "KB2", "short_description": "Empty body", "workflow_state": "Published", "text": None}]
        docs = KB.to_docs(rows)
        self.assertEqual([d.id for d in docs], ["KB1"]); self.assertEqual(docs[0].version, "1.0"); self.assertEqual(docs[0].owner_team, "Known Error")

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


class EmbeddingModelChainTests(unittest.TestCase):
    """Vectors from different models are not comparable: a query and its passages must always come from one model."""
    def _patched(self, working):
        from unittest import mock
        calls = []
        def embed(texts, kind="passage", timeout=20, model=None):
            calls.append((model, kind))
            if model not in working: return None
            return [[1.0, 0.0] if "cpu" in t else [0.0, 1.0] for t in texts]
        return calls, mock.patch.object(vectors.llm, "embed", embed), mock.patch.object(vectors.llm, "embed_models", lambda: ["m-main", "m-backup"])

    def test_falls_back_to_the_next_model_and_pins_query_and_passages_to_it(self):
        calls, p1, p2 = self._patched(working={"m-backup"})
        with p1, p2:
            vi = vectors.VectorIndex(cache_path="")
            self.assertEqual(vi.scores("cpu is busy", ["cpu runbook", "disk runbook"]), [1.0, 0.0])
            self.assertEqual(vi.model, "m-backup")
            used = {m for m, k in calls if (m, k) and k in ("passage",)}
            self.assertEqual(used, {"m-backup"}, "no passage may be embedded by a model other than the one that embedded the query")
            calls.clear(); vi.scores("cpu again", ["cpu runbook", "disk runbook"])
            self.assertEqual(calls[0][0], "m-backup", "the model that worked is tried first next time")

    def test_cache_is_per_model(self):
        calls, p1, p2 = self._patched(working={"m-main", "m-backup"})
        with p1, p2:
            vi = vectors.VectorIndex(cache_path=""); vi.scores("cpu", ["a text"]); n1 = len([c for c in calls if c[1] == "passage"])
            vi.model = None
            import os; os.environ["NVIDIA_EMBED_FALLBACK_MODELS"] = ""
            try:
                vi._passages(["a text"], "m-backup")   # a different model must embed it again, never reuse the first model's vector
            finally: os.environ.pop("NVIDIA_EMBED_FALLBACK_MODELS", None)
            self.assertEqual(len([c for c in calls if c[1] == "passage"]), n1 + 1)

    def test_all_models_down_means_lexical_only(self):
        _, p1, p2 = self._patched(working=set())
        with p1, p2: self.assertIsNone(vectors.VectorIndex(cache_path="").scores("cpu", ["x"]))

    def test_floor_is_per_model_env_overridable_and_strict_for_unknown_models(self):
        import os
        vi = vectors.VectorIndex(embed=lambda t, k: None, cache_path="")
        vi.model = "nvidia/nemotron-3-embed-1b"; self.assertEqual(vi.floor(), 0.35)
        vi.model = "some-new-model"; self.assertEqual(vi.floor(), vectors.UNCALIBRATED_FLOOR)
        os.environ["FURY_EMBED_MIN"] = "0.5"
        try: self.assertEqual(vi.floor(), 0.5)
        finally: os.environ.pop("FURY_EMBED_MIN", None)
