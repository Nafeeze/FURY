"""Governed runbook retrieval (RAG-style evidence pipeline), local and dependency-free.

Pipeline (blueprint 5.4): filter by status/access BEFORE ranking -> lexical BM25 + fuzzy character-trigram retrieval ->
Reciprocal Rank Fusion -> de-duplicate -> cite. Chunks are structure-aware (header + one chunk per step), so every hit
traces to runbook id, version and section. Embeddings/pgvector slot in as a third ranker later without changing callers.

Alert triage keeps using the deterministic runbooks.match(); this module powers how-to/knowledge questions and is
measured against a golden set (evaluate())."""
import math, os, re, time
from collections import Counter
from . import runbooks as RB

# Conversational filler that appears in questions but says nothing about the problem; it must not create a "match".
CHAT_STOP = {"how", "do", "does", "did", "i", "we", "you", "can", "could", "should", "my", "our", "fix", "solve", "handle", "deal", "help",
             "please", "problem", "issue", "error", "what", "why", "when", "where", "which", "safely", "need", "want", "get", "make", "show", "tell"}
def _stem(w):
    for suf in ("ations", "ation", "ating", "ates", "ate", "ings", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4: return w[:-len(suf)]
    return w
def _norm(text): return re.sub(r"\b(?:level|tier|line)[ \-]*([0-9])\b", r"l\1", text.lower())   # "level 2" == "L2" (support-tier vocabulary)
def _toks(text): return {_stem(t) for t in RB.tokens(_norm(text))}
def qtokens(query): return {_stem(t) for t in RB.tokens(_norm(query)) if t not in CHAT_STOP}

def _age_days(updated_at):
    try: return (time.time() - time.mktime(time.strptime(updated_at, "%Y-%m-%d"))) / 86400
    except (ValueError, TypeError): return None

def chunks(rbs):
    """Structure-aware chunks. Each carries provenance so a citation can always be rebuilt."""
    out = []
    for rb in rbs:
        base = dict(runbook=rb.id, version=rb.version, updated_at=rb.updated_at, source=rb.source, title=rb.title, owner=rb.owner_team,
                    status=rb.status, access=rb.access, content_hash=rb.content_hash)
        out.append({**base, "section": "overview", "text": f"{rb.title}. {rb.symptoms}"})
        for s in rb.steps: out.append({**base, "section": f"step {s.n}", "text": f"{s.text} {s.tool} {s.success}"})
    return out

def _grams(text):
    t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "
    return Counter(t[i:i + 3] for i in range(len(t) - 2))

def _cos(a, b):
    num = sum(v * b.get(k, 0) for k, v in a.items())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0

def _bm25(docs, query, k1=1.5, b=0.75):
    toks = [list(_toks(d["text"])) for d in docs]; q = qtokens(query)
    n = len(docs); avg = sum(len(t) for t in toks) / n if n else 1; df = Counter(w for t in toks for w in set(t))
    scores = []
    for t in toks:
        tf = Counter(t); s = 0.0
        for w in q:
            if w in tf: s += math.log(1 + (n - df[w] + .5) / (df[w] + .5)) * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return scores

def _ranks(scores):
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return {i: r for r, i in enumerate(order, 1) if scores[i] > 0}

def search(rbs, query, role="engineer", k=3, rrf_k=60, vectors=None):
    """Return up to k cited hits, best first. role gates access; retired/draft content is never returned."""
    pool = [c for c in chunks(rbs) if c["status"] == "active" and ("all" in c["access"] or role in c["access"])]
    if not pool or not query.strip(): return []
    ls = _bm25(pool, query); qg = _grams(query); fs = [_cos(qg, _grams(c["text"])) for c in pool]
    # Relevance floor: a chunk needs a real lexical match or a strong fuzzy match. Otherwise say "no evidence" (empty result)
    # instead of returning the least-bad chunk, which would be cited as if it supported the answer.
    min_fuzzy = float(os.getenv("FURY_RETRIEVAL_MIN_FUZZY", "0.45"))
    q = qtokens(query); min_cov = float(os.getenv("FURY_RETRIEVAL_MIN_COVERAGE", "0.34"))
    cov = [(len(q & _toks(c["text"])) / len(q)) if q else 0 for c in pool]      # share of the question's own terms present in the chunk
    ok = [(ls[i] > 0 and cov[i] >= min_cov) or fs[i] >= min_fuzzy for i in range(len(pool))]
    # Optional third ranker: embedding similarity. Same governance: a chunk must clear the semantic floor to count as evidence.
    sem = None
    if vectors is not None:
        vs = vectors.scores(query, [c["text"] for c in pool])
        if vs is not None:
            vmin = float(os.getenv("FURY_EMBED_MIN", "0.55"))
            ok = [ok[i] or vs[i] >= vmin for i in range(len(pool))]
            sem = _ranks([v if vs[i] >= vmin else 0 for i, v in enumerate(vs)])
    lex = _ranks([s if ok[i] else 0 for i, s in enumerate(ls)]); fuzzy = _ranks([s if ok[i] else 0 for i, s in enumerate(fs)])
    fused = Counter()
    for ranking in (lex, fuzzy) + ((sem,) if sem else ()):
        for i, r in ranking.items(): fused[i] += 1.0 / (rrf_k + r)
    max_age = float(os.getenv("FURY_RUNBOOK_MAX_AGE_DAYS", "365")); hits, seen = [], set()
    for i, score in fused.most_common():
        c = pool[i]
        if (c["runbook"], c["section"]) in seen: continue
        seen.add((c["runbook"], c["section"])); age = _age_days(c["updated_at"])
        hits.append({**c, "score": round(score, 5), "stale": bool(age is not None and age > max_age),
                     "citation": f"{c['runbook']} v{c['version']} / {c['section']}"})
        if len(hits) == k: break
    return hits

def evaluate(rbs, golden, k=3, role="engineer"):
    """golden: [(query, expected_runbook_id)]. Returns Recall@k, Precision@k, MRR and the misses."""
    rec = mrr = prec = 0.0; misses = []
    for q, want in golden:
        ids = [h["runbook"] for h in search(rbs, q, role=role, k=k)]
        if want in ids: rec += 1; mrr += 1 / (ids.index(want) + 1)
        else: misses.append((q, want, ids))
        prec += (ids.count(want) / len(ids)) if ids else 0
    n = len(golden)
    return {"queries": n, f"recall@{k}": round(rec / n, 3), f"precision@{k}": round(prec / n, 3), "mrr": round(mrr / n, 3), "misses": misses}
