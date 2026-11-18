"""Durable operational state (SQLite). Optional: pass a Store to keep run history and remediation
attempt counts across restarts. Without one, FURY behaves exactly as before (in-memory only).

Only operational facts are stored (run summaries, attempt counters) - never secrets or approval tokens."""
import json, sqlite3, threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, at TEXT, state TEXT, data TEXT NOT NULL, updated REAL DEFAULT (julianday('now')));
CREATE TABLE IF NOT EXISTS attempts (ticket TEXT, action TEXT, n INTEGER NOT NULL, PRIMARY KEY (ticket, action));
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

    def recover_interrupted(self):
        """Runs that were queued/running when the process died can never finish: mark them failed, visibly."""
        n = 0
        for r in self.load_runs(1000).values():
            if r.get("state") in ("queued", "running"):
                r.update(state="error", out={"outcome": "error", "reason": "interrupted by restart"}); self.save_run(r); n += 1
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

    def close(self):
        with self._lock: self._db.close()
