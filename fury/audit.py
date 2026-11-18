"""Tamper-evident audit log. Every security-relevant event (who did what, allowed or denied) is appended as a JSON line
whose hash covers the previous line's hash, so editing, deleting or reordering any past entry breaks verification.

This detects tampering; it does not prevent it. For enterprise use, ship the file to write-once storage (e.g. S3 Object
Lock / SIEM) and keep the head hash somewhere the writer cannot modify. Free text is redacted before it is stored, and
secrets/tokens must never be passed in `detail`."""
import hashlib, json, os, threading, time
from . import telemetry

GENESIS = "0" * 64


def _digest(prev, body):
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest()


class Audit:
    def __init__(self, path=None, keep=5000):
        self.path, self.keep, self._lock = path, keep, threading.Lock()
        self.entries, self._head = [], GENESIS
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        e = json.loads(line); self.entries.append(e); self._head = e["hash"]
            self.entries = self.entries[-keep:]

    def record(self, actor, action, outcome, **detail):
        """actor: who (user/role/system); action: what (e.g. 'chat.report'); outcome: allowed|denied|ok|error."""
        clean = {k: (telemetry.redact(v, 200) if isinstance(v, str) else v) for k, v in detail.items()}
        body = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": str(actor)[:80], "action": action, "outcome": outcome, "detail": clean}
        with self._lock:
            h = _digest(self._head, body)
            entry = {**body, "prev": self._head, "hash": h}
            self.entries.append(entry); self.entries = self.entries[-self.keep:]; self._head = h
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, sort_keys=True) + "\n")
        telemetry.counter("audit.event", action=action, outcome=outcome)
        return entry

    @property
    def head(self):
        return self._head

    def recent(self, limit=50):
        return list(reversed(self.entries[-limit:]))

    @staticmethod
    def verify_file(path):
        """Re-walk the whole file. Returns (ok, entries_checked, first_bad_line or None)."""
        prev, n = GENESIS, 0
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate((l for l in f if l.strip()), 1):
                e = json.loads(line); body = {k: e[k] for k in ("at", "actor", "action", "outcome", "detail")}
                if e["prev"] != prev or e["hash"] != _digest(prev, body):
                    return False, n, n
                prev = e["hash"]
        return True, n, None


NULL = Audit(None, keep=1)   # used when auditing is not configured: records nothing durable but never crashes callers
