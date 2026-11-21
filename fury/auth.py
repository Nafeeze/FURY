"""API authentication and role-based access control.

  python -m fury.auth new <name> <viewer|operator|admin>     # prints the token ONCE and the env entry to store

Tokens are random 256-bit secrets; only their SHA-256 hash is configured (FURY_API_TOKENS="name:role:hash,..."), so the
config never contains a usable credential. Roles: viewer (read), operator (viewer + chat/report), admin (everything,
incl. audit log, approval history, demo controls). Anything not explicitly allowed for a lower role needs admin
(deny by default). The static page itself is public; every /api/ route requires a token when auth is enabled.

Failed attempts are throttled per client and every denial is audited. This is a token gate suitable for a prototype or
an internal tool; for enterprise SSO put FURY behind your identity-aware proxy and map its identity to these roles."""
import hashlib, hmac, os, secrets, sys, time
from collections import defaultdict, deque

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


def parse_tokens(spec):
    """'name:role:sha256hex,...' -> list of dicts. Raises ValueError on ANY malformed entry (fail closed at startup)."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            raise ValueError("FURY_API_TOKENS entries must look like name:role:sha256hex")
        name, role, h = (b.strip() for b in bits)
        if not name or role not in RANK or len(h) != 64 or any(c not in "0123456789abcdef" for c in h.lower()):
            raise ValueError(f"invalid FURY_API_TOKENS entry for '{name or '?'}' (role must be one of {ROLES}, hash must be 64 hex chars)")
        out.append({"name": name, "role": role, "hash": h.lower()})
    return out


def required_role(method, path):
    """Minimum role for a route, or None if public. Unknown routes/methods need admin."""
    if not path.startswith("/api/"):
        return None
    if path in ("/api/audit", "/api/approvals/history") or path.startswith("/api/demo/"):
        return "admin"
    if method == "GET":
        return "viewer"
    if method == "POST" and path in ("/api/chat", "/api/chat/reset", "/api/voice/session", "/api/voice/turn"):
        return "operator"
    return "admin"


class Auth:
    def __init__(self, entries, audit=None, max_failures=10, window=60.0, clock=time.monotonic, chat_per_min=None, api_per_min=None):
        self.entries, self.audit, self.max_failures, self.window, self._clock = entries, audit, max_failures, window, clock
        self._fails = defaultdict(deque)
        # Per-identity request limits (sliding 60 s window). Chat is tight because every message can call the paid model.
        self.chat_per_min = int(os.getenv("FURY_RATE_CHAT_PER_MIN", "20")) if chat_per_min is None else chat_per_min
        self.api_per_min = int(os.getenv("FURY_RATE_API_PER_MIN", "600")) if api_per_min is None else api_per_min
        self._hits = defaultdict(deque)

    def _rate_ok(self, name, path):
        """Sliding-window limit per identity and bucket. Returns True if the request may proceed; a limit of 0 disables it."""
        bucket, limit = ("chat", self.chat_per_min) if path == "/api/chat" else ("api", self.api_per_min)
        if limit <= 0:
            return True
        q, now = self._hits[(name, bucket)], self._clock()
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _record(self, actor, outcome, **detail):
        if self.audit:
            self.audit.record(actor, "api.access", outcome, **detail)

    def _throttled(self, client):
        q, now = self._fails[client], self._clock()
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.max_failures

    def check(self, method, path, header, client="?"):
        """Returns (ok, http_status, user_or_None, error_message)."""
        need = required_role(method, path)
        if need is None:
            return True, 200, None, ""
        who = hashlib.sha256(str(client).encode()).hexdigest()[:8]
        if self._throttled(who):
            self._record(f"client:{who}", "denied", reason="too many failed attempts", path=path)
            return False, 429, None, "too many failed attempts; try again later"
        token = header[7:].strip() if header and header.lower().startswith("bearer ") else ""
        user = None
        if token:
            h = hash_token(token)
            for e in self.entries:                         # compare against every entry: no early exit on a timing difference
                if hmac.compare_digest(h, e["hash"]):
                    user = e
        if not user:
            self._fails[who].append(self._clock())
            self._record(f"client:{who}", "denied", reason="missing or invalid token" if not token else "invalid token", path=path)
            return False, 401, None, "authentication required"
        if RANK[user["role"]] < RANK[need]:
            self._record(user["name"], "denied", reason=f"role {user['role']} lacks {need}", method=method, path=path)
            return False, 403, user, f"requires role '{need}'"
        if not self._rate_ok(user["name"], path):
            self._record(user["name"], "denied", reason="rate limit", method=method, path=path)
            return False, 429, user, "rate limit reached; slow down and retry in a minute"
        return True, 200, user, ""


def from_env(audit=None):
    """Auth instance from FURY_API_TOKENS, or None when unset (auth disabled)."""
    entries = parse_tokens(os.getenv("FURY_API_TOKENS", ""))
    return Auth(entries, audit) if entries else None


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "new" and sys.argv[3] in RANK:
        tok = new_token()
        print(f"Token for {sys.argv[2]} ({sys.argv[3]}): {tok}\n(shown once; give it to the user, never commit it)\n")
        print(f"Add to FURY_API_TOKENS (comma-separate multiple):\n{sys.argv[2]}:{sys.argv[3]}:{hash_token(tok)}")
    else:
        sys.exit("usage: python -m fury.auth new <name> <viewer|operator|admin>")
