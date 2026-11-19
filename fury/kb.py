"""ServiceNow knowledge-base ingestion.

Pulls PUBLISHED articles from the instance and turns them into the same chunk shape the governed retrieval pipeline already
uses, so how-to / "why" questions can be answered from the organisation's own knowledge.

Governance (deliberate):
- KB articles are INFORMATIONAL. They are searched only to answer questions; they are never given to the Knowledge agent, so
  they can never select or drive a change on the server. Only runbooks (human-approved, versioned, in the repo) can do that.
- Only workflow_state=published articles are ingested; drafts, retired and expired articles never enter the index.
- Every answer built from an article cites its number and version.
- Failure to reach ServiceNow keeps the previous index (stale-but-available beats empty) and is reported, never raised."""
import hashlib, html, re
from . import runbooks as RB, telemetry

MAX_ARTICLES = 200
CHUNK_CHARS = 600


def to_text(markup):
    """Plain text from an article body: drop scripts/styles/tags, keep paragraph breaks, unescape entities."""
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup or "")
    t = re.sub(r"(?i)</?(p|br|li|h[1-6]|div|tr)[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return "\n".join(" ".join(line.split()) for line in t.splitlines() if line.strip())


def _paragraphs(text):
    """Split into chunks of about CHUNK_CHARS without cutting a sentence in half where possible."""
    out, cur = [], ""
    for para in text.splitlines():
        if cur and len(cur) + len(para) > CHUNK_CHARS: out.append(cur); cur = ""
        cur = (cur + " " + para).strip()
    if cur: out.append(cur)
    return out


def to_docs(articles):
    """ServiceNow rows -> pseudo-runbooks accepted by retrieval.chunks()/search(). Unpublished rows are dropped."""
    docs = []
    for a in articles[:MAX_ARTICLES]:
        if str(a.get("workflow_state", "")).lower() != "published": continue
        body = to_text(a.get("text", "")); title = (a.get("short_description") or "").strip()
        if not title or not body: continue
        parts = _paragraphs(body)
        base = a.get("kb_knowledge_base")
        docs.append(RB.Runbook(
            id=a.get("number") or title, title=title, kind="kb", symptoms=parts[0], owner_team=(base.get("display_value") if isinstance(base, dict) else base) or "knowledge base",
            escalate_to="", steps=[RB.Step(n=i + 1, type="info", text=p) for i, p in enumerate(parts[1:], 1)],
            version=str(a.get("version") or "1"), status="active", updated_at=(a.get("sys_updated_on") or "")[:10], source=f"servicenow-kb:{a.get('number')}",
            content_hash=hashlib.sha256((title + body).encode("utf-8")).hexdigest()[:12], access=("all",)))
    return docs


class KnowledgeBase:
    """Holds the current ingested articles. refresh() is safe to call from a timer or an admin endpoint."""
    def __init__(self, snow):
        self.snow, self.docs, self.last_ok, self.last_error = snow, [], None, ""

    def refresh(self):
        fetch = getattr(self.snow, "list_kb", None)
        if fetch is None:
            self.last_error = "this ServiceNow client cannot list knowledge articles"; return 0
        try:
            self.docs = to_docs(fetch(MAX_ARTICLES)); self.last_ok, self.last_error = __import__("time").strftime("%Y-%m-%d %H:%M:%S"), ""
            telemetry.counter("kb.refresh", result="ok")
        except Exception as e:
            self.last_error = f"{type(e).__name__}"; telemetry.counter("kb.refresh", result="error")
        return len(self.docs)
