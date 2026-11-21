"""Semantic (embedding) ranker for the governed retrieval pipeline.

Third ranker beside BM25 and fuzzy trigram matching: it catches paraphrases that share no words with a runbook
("the box feels sluggish" ~ "CPU is pegged"). It never replaces the lexical rankers and never bypasses governance:
status/access filtering still happens before ranking, and a hit must still clear a relevance floor, otherwise FURY abstains.

Embeddings come from `llm.embed` (hosted model). Passage vectors are cached by text hash (in memory, optionally on disk via
FURY_VECTOR_CACHE) so each runbook chunk is embedded once. If the embedding service is down, `scores()` returns None and the
caller silently keeps lexical search: semantic search can only add recall, never remove availability."""
import hashlib, json, math, os, threading
from . import llm, telemetry


# Relevance floor per embedding model (cosine similarity a chunk must reach to count as evidence). Scores are only comparable
# within one model, so each model carries its own floor, set from tests/calibrate_embeddings.py. FURY_EMBED_MIN overrides all.
# A model that has not been calibrated gets the strictest floor, so an unknown model can only abstain, never invent evidence.
EMBED_FLOORS = {
    # 2026-11-19: 12 paraphrases / 15 out-of-scope questions. Floor 0.35 = 7 of 12 paraphrases found (lexical alone: 2), 0 of 15 false matches.
    # Out-of-scope maximum was 0.309, so 0.35 keeps a margin. A small, author-written set: re-run tests/calibrate_embeddings.py with real queries.
    "nvidia/nemotron-3-embed-1b": 0.35,
}
UNCALIBRATED_FLOOR = 0.60

def _h(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


class VectorIndex:
    def __init__(self, embed=None, cache_path=None):
        self._custom = embed is not None          # an injected embedder (tests, other providers) is used as-is: one model, no chain
        self.embed = embed or llm.embed
        self.model = None                         # the model that last worked; tried first next time
        self.cache_path = cache_path if cache_path is not None else (os.getenv("FURY_VECTOR_CACHE") or None)
        self._cache, self._lock = {}, threading.Lock()
        if self.cache_path and os.path.exists(self.cache_path):
            try: self._cache = json.load(open(self.cache_path, encoding="utf-8"))
            except (OSError, ValueError): self._cache = {}

    def floor(self):
        """The relevance floor for the model that produced the last scores."""
        env = os.getenv("FURY_EMBED_MIN")
        if env: return float(env)
        return EMBED_FLOORS.get(self.model, UNCALIBRATED_FLOOR)

    def _models(self):
        if self._custom: return [None]
        chain = llm.embed_models()
        return ([self.model] if self.model in chain else []) + [m for m in chain if m != self.model]

    def _emb(self, texts, kind, model): return self.embed(texts, kind) if model is None else self.embed(texts, kind, model=model)

    def _passages(self, texts, model=None):
        """Vectors for texts from ONE model, embedding only the ones not cached (cache keys include the model). None if unavailable."""
        key = lambda t: _h(f"{model}|{t}")
        with self._lock:
            missing = [t for t in dict.fromkeys(texts) if key(t) not in self._cache]
            if missing:
                vecs = self._emb(missing, "passage", model)
                if not vecs: return None
                for t, v in zip(missing, vecs): self._cache[key(t)] = v
                if self.cache_path:
                    try: json.dump(self._cache, open(self.cache_path, "w", encoding="utf-8"))
                    except OSError: pass
            return [self._cache[key(t)] for t in texts]

    def scores(self, query, texts):
        """Cosine similarity of query to each text, or None when embeddings are unavailable. Tries each model in turn; a query and its
        passages always come from the same model."""
        if not texts or not query.strip(): return None
        for model in self._models():
            qv = self._emb([query], "query", model)
            pv = self._passages(texts, model) if qv else None
            if qv and pv:
                if model != self.model: telemetry.counter("retrieval.semantic.model_switch", model=str(model))
                self.model = model
                telemetry.counter("retrieval.semantic", result="ok")
                return [cosine(qv[0], v) for v in pv]
        telemetry.counter("retrieval.semantic", result="unavailable"); return None
