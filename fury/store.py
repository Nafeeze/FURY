"""Durable operational state (SQLite). Optional: pass a Store to keep run history and remediation
attempt counts across restarts. Without one, FURY behaves exactly as before (in-memory only).

Only operational facts are stored (run summaries, attempt counters) - never secrets or approval tokens."""
import json, sqlite3, threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, at TEXT, state TEXT, data TEXT NOT NULL, updated REAL DEFAULT (julianday('now')));
CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT, action TEXT, target TEXT, tier TEXT, status TEXT NOT NULL,
  approver TEXT, detail TEXT, requested_at TEXT DEFAULT (datetime('now')), decided_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS attempts (ticket TEXT, action TEXT, n INTEGER NOT NULL, PRIMARY KEY (ticket, action));
CREATE TABLE IF NOT EXISTS inflight (ticket TEXT, action TEXT, at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ticket, action));
"""

class Store:
    def __init__(self, path=":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA); self._db.commit()

    # ---- runs ----
    def save_run(self, run):
        # start/end index into the in-memory bus trace, which does not survive a restart; keep summary only.
        data = {k: v for k, v in run.items() if k not in ("start", "end")}
        with self._lock:
            self._db.execute("INSERT INTO runs (id, at, state, data) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                             "state=excluded.state, data=excluded.data, updated=julianday('now')",
                             (run["id"], run.get("at"), run.get("state"), json.dumps(data, default=str)))
            self._db.commit()

    def load_runs(self, limit=200):
        with self._lock:
            rows = self._db.execute("SELECT data FROM runs ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        runs = {}
        for (d,) in rows:
            r = json.loads(d); r.setdefault("start", None); r.setdefault("end", None); runs[r["id"]] = r
        return runs

    def recover_interrupted(self, resume_window=0, max_resumes=1):
        """Runs that were queued/running when the process died. By default they are marked failed, visibly.
        With resume_window > 0 (seconds), a run that is recent enough, still holds its saved input and has not already been
        resumed max_resumes times is marked 'interrupted' instead, for Ctx.resume_interrupted() to continue from its checkpoint.
        Returns how many runs were failed (not resumable)."""
        import time
        n = 0
        for r in self.load_runs(1000).values():
            if r.get("state") in ("queued", "running"):
                resumable = (resume_window > 0 and isinstance(r.get("input"), dict) and time.time() - float(r.get("ts") or 0) <= resume_window
                             and int(r.get("resumes", 0)) < max_resumes)
                if resumable: r.update(state="interrupted")
                else: r.pop("input", None); r.update(state="error", out={"outcome": "error", "reason": "interrupted by restart"}); n += 1
                self.save_run(r)
        # An approval that was still waiting when the process died can never be honoured: the signed token only
        # exists in memory and the waiting workflow is gone. Close it so nobody believes it is still pending.
        with self._lock:
            self._db.execute("UPDATE approvals SET status='expired', detail='interrupted by restart', decided_at=datetime('now') WHERE status='requested'")
            self._db.commit()
        return n

    # ---- remediation attempts (one-attempt-then-escalate must survive a restart) ----
    def attempts(self, ticket, action):
        with self._lock:
            row = self._db.execute("SELECT n FROM attempts WHERE ticket=? AND action=?", (ticket, action)).fetchone()
        return row[0] if row else 0

    def record_attempt(self, ticket, action):
        with self._lock:
            self._db.execute("INSERT INTO attempts (ticket, action, n) VALUES (?,?,1) ON CONFLICT(ticket, action) DO UPDATE SET n=n+1", (ticket, action))
            self._db.commit()

    # ---- in-flight marker: set just before an action is sent to the server, cleared once its outcome is known ----
    def mark_inflight(self, ticket, action):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO inflight (ticket, action) VALUES (?,?)", (ticket, action)); self._db.commit()

    def clear_inflight(self, ticket, action):
        with self._lock:
            self._db.execute("DELETE FROM inflight WHERE ticket=? AND action=?", (ticket, action)); self._db.commit()

    def inflight(self, ticket, action):
        with self._lock:
            return self._db.execute("SELECT 1 FROM inflight WHERE ticket=? AND action=?", (ticket, action)).fetchone() is not None

    # ---- approval / execution audit trail: requested -> approved|rejected|expired|denied -> executed|failed ----
    def approval_open(self, ticket, action, target, tier):
        with self._lock:
            cur = self._db.execute("INSERT INTO approvals (ticket, action, target, tier, status) VALUES (?,?,?,?, 'requested')", (ticket, action, target, tier))
            self._db.commit(); return cur.lastrowid

    def approval_update(self, aid, status, approver=None, detail=None):
        col = "decided_at" if status in ("approved", "rejected", "expired", "denied") else "finished_at"
        with self._lock:
            self._db.execute(f"UPDATE approvals SET status=?, approver=COALESCE(?, approver), detail=COALESCE(?, detail), {col}=datetime('now') WHERE id=?",  # nosec B608: col is one of two fixed literals chosen above, never user input
                             (status, approver, (detail or None) and str(detail)[:300], aid))
            self._db.commit()

    def approvals_history(self, limit=50):
        with self._lock:
            rows = self._db.execute("SELECT id,ticket,action,target,tier,status,approver,detail,requested_at,decided_at,finished_at FROM approvals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "ticket", "action", "target", "tier", "status", "approver", "detail", "requested_at", "decided_at", "finished_at")
        return [dict(zip(keys, r)) for r in rows]

    def prune(self, days):
        """Retention: delete runs and approval records older than `days`. Returns counts. days<=0 keeps everything."""
        if days <= 0: return {"runs": 0, "approvals": 0}
        with self._lock:
            r = self._db.execute("DELETE FROM runs WHERE updated < julianday('now') - ?", (days,)).rowcount
            a = self._db.execute("DELETE FROM approvals WHERE requested_at < datetime('now', ?)", (f"-{int(days)} days",)).rowcount
            self._db.commit()
        return {"runs": r, "approvals": a}

    def close(self):
        with self._lock: self._db.close()
