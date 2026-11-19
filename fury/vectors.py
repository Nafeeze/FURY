"""Semantic (embedding) ranker for the governed retrieval pipeline.

Third ranker beside BM25 and fuzzy trigram matching: it catches paraphrases that share no words with a runbook
("the box feels sluggish" ~ "CPU is pegged"). It never replaces the lexical rankers and never bypasses governance:
status/access filtering still happens before ranking, and a hit must still clear a relevance floor, otherwise FURY abstains.

Embeddings come from `llm.embed` (hosted model). Passage vectors are cached by text hash (in memory, optionally on disk via
FURY_VECTOR_CACHE) so each runbook chunk is embedded once. If the embedding service is down, `scores()` returns None and the
caller silently keeps lexical search: semantic search can only add recall, never remove availability."""
import hashlib, json, math, os, threading
from . import llm, telemetry


def _h(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


class VectorIndex:
    def __init__(self, embed=None, cache_path=None):
        self.embed = embed or llm.embed
        self.cache_path = cache_path if cache_path is not None else (os.getenv("FURY_VECTOR_CACHE") or None)
        self._cache, self._lock = {}, threading.Lock()
        if self.cache_path and os.path.exists(self.cache_path):
            try: self._cache = json.load(open(self.cache_path, encoding="utf-8"))
            except (OSError, ValueError): self._cache = {}

    def _passages(self, texts):
        """Vectors for texts, embedding only the ones not cached. None if any are unavailable."""
        with self._lock:
            missing = [t for t in dict.fromkeys(texts) if _h(t) not in self._cache]
            if missing:
                vecs = self.embed(missing, "passage")
                if not vecs: return None
                for t, v in zip(missing, vecs): self._cache[_h(t)] = v
                if self.cache_path:
                    try: json.dump(self._cache, open(self.cache_path, "w", encoding="utf-8"))
                    except OSError: pass
            return [self._cache[_h(t)] for t in texts]

    def scores(self, query, texts):
        """Cosine similarity of query to each text, or None when embeddings are unavailable."""
        if not texts or not query.strip(): return None
        qv = self.embed([query], "query")
        pv = self._passages(texts) if qv else None
        if not qv or not pv:
            telemetry.counter("retrieval.semantic", result="unavailable"); return None
        telemetry.counter("retrieval.semantic", result="ok")
        return [cosine(qv[0], v) for v in pv]
