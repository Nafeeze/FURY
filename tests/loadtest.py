"""Load test: drive the real Flask app (fake ServiceNow / server / Slack, real auth) with concurrent clients and report
throughput, latency percentiles and error rates.   python tests/loadtest.py [--clients 20] [--seconds 10]
Measures FURY's own overhead (routing, auth, agents, telemetry). It deliberately does NOT measure ServiceNow, AWS or Slack
latency: those need a separate test against the live systems with their owners' agreement."""
import argparse, os, statistics, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fury import system, auth as A
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.audit import Audit
from fury.webapp import Ctx, create_app

ap = argparse.ArgumentParser(); ap.add_argument("--clients", type=int, default=20); ap.add_argument("--seconds", type=float, default=10)
a = ap.parse_args()
os.environ["FURY_CHAT_MODE"] = "full"
tok = "load-test-token"
approver = "U0LOAD"
tgt, snow = FakeTarget(), FakeSnow()
chat = FakeChat([(approver, "approve")] * 500)
bus, triage, _ = system.build(target=tgt, snow=snow, chat=chat, approvers=[approver])
ctx = Ctx(bus, triage, snow, chat, "i-load", tgt, audit=Audit())
app = create_app(ctx, demo=True, auth=A.Auth(A.parse_tokens("load:admin:" + A.hash_token(tok)), max_failures=10**9))
H = {"Authorization": "Bearer " + tok}
lat, codes, lock = [], {}, threading.Lock()
MIX = [("GET", "/api/health", None), ("GET", "/api/runs", None), ("GET", "/api/status", None), ("GET", "/api/obs/overview", None),
       ("POST", "/api/chat", {"message": "how do I check disk usage?"}), ("POST", "/api/chat", {"message": "list open incidents"}),
       ("POST", "/api/demo/inject/disk_full", None)]
stop = time.monotonic() + a.seconds

def worker(i):
    c = app.test_client(); n = i
    while time.monotonic() < stop:
        m, p, body = MIX[n % len(MIX)]; n += 1
        t = time.perf_counter()
        r = c.get(p, headers=H) if m == "GET" else c.post(p, headers={**H, "X-Fury-Session": f"s{i}"}, json=body)
        ms = (time.perf_counter() - t) * 1000
        with lock: lat.append(ms); codes[r.status_code] = codes.get(r.status_code, 0) + 1

ts = [threading.Thread(target=worker, args=(i,)) for i in range(a.clients)]
t0 = time.monotonic(); [t.start() for t in ts]; [t.join() for t in ts]; el = time.monotonic() - t0
lat.sort(); pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]
bad = sum(v for k, v in codes.items() if k >= 500)
print(f"clients={a.clients} duration={el:.1f}s requests={len(lat)} throughput={len(lat)/el:.0f} req/s")
print(f"latency ms  p50={pct(.5):.1f}  p95={pct(.95):.1f}  p99={pct(.99):.1f}  max={lat[-1]:.1f}")
print(f"status codes {dict(sorted(codes.items()))}   server errors (5xx): {bad}")
sys.exit(1 if bad else 0)
