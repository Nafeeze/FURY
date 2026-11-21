"""Calibrate the semantic-search relevance floor, per embedding model.   python tests/calibrate_embeddings.py [model ...]

Needs NVIDIA_API_KEY (reads .env). For each model it measures two things against the runbooks in ./runbooks:
  - paraphrases that share no words with a runbook (should be found), scored by their best chunk of the CORRECT runbook
  - out-of-scope questions (must NOT be found), scored by their best chunk of ANY runbook
and prints, for a range of floors, how many paraphrases are caught and how many out-of-scope questions would wrongly match.
Pick the lowest floor with zero false matches and put it in fury/vectors.py EMBED_FLOORS. Re-run when the model or the runbooks change.
Add real queries to POS / NEG as they appear: the more real examples, the more trustworthy the floor."""
import os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
from run_fury import load_env; load_env()
from fury import llm, retrieval, runbooks as RB, vectors

POS = [  # (query, runbook it should lead to): no deliberate word overlap with the runbooks
    ("customers cannot load the storefront", "AWS-SVC-001"), ("the web server stopped answering requests", "AWS-SVC-001"),
    ("visitors get a service unavailable page", "AWS-SVC-001"), ("the application host is unresponsive over http", "AWS-SVC-001"),
    ("the machine feels sluggish", "AWS-CPU-001"), ("something is hogging the processor", "AWS-CPU-001"), ("processing power is exhausted", "AWS-CPU-001"),
    ("running low on storage", "AWS-DISK-001"), ("the volume has no room left", "AWS-DISK-001"), ("files cannot be saved because the drive is full", "AWS-DISK-001"),
    ("who takes over when I cannot solve it", "INC-ESC-001"), ("when do I pass this to the senior team", "INC-ESC-001"),
]
NEG = ["memory leak", "how to bake a cake", "database replication lag", "ssl certificate expired", "what is the capital of France", "my cat is sick",
       "the printer is jammed", "reset my email password", "vpn will not connect", "order lunch for the team", "how do I write a python function",
       "which holiday is next month", "wifi is slow in the meeting room", "the monitor is flickering", "backup job failed last night"]

def main(models):
    rbs = RB.load(os.path.join(ROOT, "runbooks")); ch = retrieval.chunks(rbs); texts = [c["text"] for c in ch]
    lex = sum(1 for q, w in POS if any(h["runbook"] == w for h in retrieval.search(rbs, q, k=3)))
    print(f"lexical search alone: paraphrases found {lex}/{len(POS)}, out-of-scope wrongly answered {sum(1 for q in NEG if retrieval.search(rbs, q, k=3))}/{len(NEG)}")
    for model in models or llm.embed_models():
        os.environ["NVIDIA_EMBED_MODEL"], os.environ["NVIDIA_EMBED_FALLBACK_MODELS"] = model, ""
        vi = vectors.VectorIndex(cache_path="")
        def scores(q):
            for _ in range(4):
                s = vi.scores(q, texts)
                if s is not None: return s
                time.sleep(2)
        pos, neg, fail = [], [], 0
        for q, want in POS:
            s = scores(q)
            if s is None: fail += 1; continue
            pos.append(max(v for v, c in zip(s, ch) if c["runbook"] == want))
        for q in NEG:
            s = scores(q)
            if s is None: fail += 1; continue
            neg.append(max(s))
        if not pos or not neg: print(f"\n{model}: UNAVAILABLE right now ({fail} failed calls)"); continue
        print(f"\n{model}  failed calls {fail}  out-of-scope max {max(neg):.3f}  paraphrase median {sorted(pos)[len(pos) // 2]:.3f}")
        for t in [x / 100 for x in range(20, 66, 5)]:
            print(f"  floor {t:.2f}: paraphrases caught {sum(v >= t for v in pos)}/{len(pos)}   false matches {sum(v >= t for v in neg)}/{len(neg)}")

if __name__ == "__main__": main(sys.argv[1:])
