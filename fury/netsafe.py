"""Outbound URL safety. FURY only ever talks to ServiceNow, the model API and other FURY agents over HTTP(S); a mistyped or
tampered configuration must not make it open file:// or other schemes. https is required, http is allowed only for loopback."""
import urllib.parse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def require_http(url):
    """Return url unchanged (without a trailing slash) or raise ValueError."""
    u = urllib.parse.urlparse((url or "").strip())
    if u.scheme == "https" and u.hostname: return url.rstrip("/")
    if u.scheme == "http" and u.hostname in LOOPBACK: return url.rstrip("/")
    raise ValueError(f"refusing URL with scheme '{u.scheme or 'none'}': https is required (http only for localhost)")
