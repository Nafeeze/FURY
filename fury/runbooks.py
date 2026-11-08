import re
from dataclasses import dataclass, field
from pathlib import Path

STOP = {"a","an","the","on","in","of","is","are","above","below","percent","usage","high","server","instance","alarm","fury",
        "i","and","or","to","for","with","at","by","has","have","been","was","state","metric","low","over","than"}
def tokens(text): return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOP}

@dataclass
class Step:
    n: int; type: str; text: str; tool: str = ""; params: dict = field(default_factory=dict); success: str = ""
    def as_dict(self): return {"n": self.n, "type": self.type, "text": self.text, "tool": self.tool, "params": self.params, "success": self.success}

@dataclass
class Runbook:
    id: str; title: str; kind: str; symptoms: str; owner_team: str; escalate_to: str; steps: list
    def tokens(self): return tokens(self.symptoms + " " + self.title)
    def as_dict(self):
        return {"id": self.id, "title": self.title, "owner_team": self.owner_team, "escalate_to": self.escalate_to,
                "steps": [s.as_dict() for s in self.steps]}

def _params(s):
    out = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1); out[k.strip()] = v.strip()
    return out

def parse(text):
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    fm = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1); fm[k.strip()] = v.strip()
    steps, cols = [], None
    for line in text.splitlines():
        if not line.startswith("|"): continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "#": cols = [c.lower() for c in cells]; continue
        if set("".join(cells)) <= set("-: "): continue
        if cols and len(cells) == len(cols):
            r = dict(zip(cols, cells))
            tool = r.get("tool / action", "")
            steps.append(Step(int(r["#"]), r["type"], r["step"], tool, _params(r.get("parameters", "")), r.get("success criteria", "")))
    return Runbook(fm.get("id", ""), fm.get("title", ""), fm.get("kind", "alert"), fm.get("symptoms", ""),
                   fm.get("owner_team", ""), fm.get("escalate_to", ""), steps)

def load(directory):
    return [parse(p.read_text(encoding="utf-8")) for p in sorted(Path(directory).glob("*.md"))]

def match(runbooks, query, min_score=2):
    q = tokens(query); best, score = None, 0
    for rb in runbooks:
        if rb.kind != "alert": continue
        s = len(q & rb.tokens())
        if s > score: best, score = rb, s
    return (best, score) if score >= min_score else (None, score)
