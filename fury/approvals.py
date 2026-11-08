"""Signed, expiring, single-purpose approval tokens. Comms signs after a trusted human approves; Remediation verifies."""
import os, json, time, hmac, hashlib, base64
def _secret(): return os.getenv("APPROVAL_SECRET", "dev-only-change-me").encode()
def sign(action, target, ticket, approver, ttl=600):
    p = json.dumps({"action": action, "target": target, "ticket": ticket, "approver": approver,
                    "exp": int(time.time()) + ttl, "nonce": os.urandom(6).hex()}, sort_keys=True)
    b = base64.urlsafe_b64encode(p.encode()).decode()
    return b + "." + hmac.new(_secret(), b.encode(), hashlib.sha256).hexdigest()
def verify(token, action, target, ticket):
    try:
        b, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_secret(), b.encode(), hashlib.sha256).hexdigest()): return None, "bad signature"
        p = json.loads(base64.urlsafe_b64decode(b))
    except Exception: return None, "malformed token"
    if p["exp"] < time.time(): return None, "approval expired"
    if (p["action"], p["target"], p["ticket"]) != (action, target, ticket): return None, "approval does not match this action/target/ticket"
    return p, None
