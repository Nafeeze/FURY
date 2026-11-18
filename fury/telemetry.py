"""
Observability for FURY: tracing + measures + redaction, in one small module.

Design goals, on purpose:
- Zero hard dependency. If the `opentelemetry` SDK is installed and
  FURY_OTEL=on, we use it for real (spans + metrics go to the console
  exporter by default, or an OTLP endpoint if OTEL_EXPORTER_OTLP_ENDPOINT
  is set). If it isn't installed, or FURY_OTEL is off, we fall back to an
  in-process recorder with the exact same call shape, so the rest of the
  codebase never has to branch on "do we have OTel or not".
- Nothing raw goes into a span or a counter label. Every piece of text
  that could contain a hostname, ticket text, approver name, or model
  output is redacted first (see redact()). Spans carry *shapes* of data
  (lengths, booleans, enums) not the data itself.
- This module is read by everything but imports nothing from the rest of
  fury/, so it can't create a circular import and is safe to import first.

Env vars:
  FURY_OTEL                 - "on" to use the real OpenTelemetry SDK if installed;
                               anything else (default) uses the built-in recorder.
  OTEL_EXPORTER_OTLP_ENDPOINT - if set (and FURY_OTEL=on and the SDK's OTLP
                               exporter is installed), spans/metrics ship there
                               instead of stdout.
  FURY_TELEMETRY_RAW         - "on" to disable redaction (debug only; never
                               use this against a real server).
"""
import os
import re
import time
import threading
import uuid
import contextvars
from contextlib import contextmanager

# ---------------------------------------------------------------- redaction
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_KV = re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|secret|authorization)\b\s*[:=]\s*\S+")
_LONGNUM = re.compile(r"\b\d{9,}\b")  # phone numbers, account numbers, card-like sequences
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def redact(text, max_len=160):
    """Best-effort scrub of a piece of free text before it's allowed anywhere
    in telemetry: a span attribute, a log line, an eval report. Not a
    substitute for keeping secrets out of the text in the first place
    (see PRIVACY.md) -- this is a safety net, not the control."""
    if not isinstance(text, str):
        return text
    if os.getenv("FURY_TELEMETRY_RAW", "off").lower() == "on":
        return text[:max_len]
    t = _AWS_KEY.sub("[aws-key]", text)
    t = _SECRET_KV.sub(lambda m: m.group(1) + "=[redacted]", t)
    t = _EMAIL.sub("[email]", t)
    t = _IPV4.sub("[ip]", t)
    t = _LONGNUM.sub("[number]", t)
    return t[:max_len]


# ------------------------------------------------------------ trace context
# Which trace/span is "current" on this thread. Child spans read it to find
# their parent, so one request becomes one tree. Worker threads start with an
# empty context, so capture()/attach() carry it across the hand-off.
_CUR = contextvars.ContextVar("fury_span", default=None)


def _new_id(n):
    return uuid.uuid4().hex[:n * 2]


def set_attr(rec, key, value):
    """Add an attribute to a live span record. Free text is redacted first."""
    rec["attrs"][key] = redact(value) if isinstance(value, str) else value


def capture():
    """Snapshot the current trace position so another thread can continue it."""
    cur = _CUR.get()
    snap = dict(cur) if cur else None
    if snap and _otel:
        try:
            from opentelemetry import context as otel_ctx
            snap["otel"] = otel_ctx.get_current()
        except Exception:
            pass
    return snap


@contextmanager
def attach(snap):
    """Continue a captured trace on this thread (no-op if snap is None)."""
    if not snap:
        yield
        return
    token = _CUR.set({"trace_id": snap["trace_id"], "span_id": snap["span_id"]})
    otel_token = None
    if snap.get("otel") is not None:
        try:
            from opentelemetry import context as otel_ctx
            otel_token = otel_ctx.attach(snap["otel"])
        except Exception:
            otel_token = None
    try:
        yield
    finally:
        if otel_token is not None:
            try:
                from opentelemetry import context as otel_ctx
                otel_ctx.detach(otel_token)
            except Exception:
                pass
        _CUR.reset(token)


# ------------------------------------------------------------- recorder(s)
class _Recorder:
    """In-process fallback: same shape as the OTel-backed version below,
    kept forever in memory (bounded) so an eval run or a /api/metrics
    endpoint can read it back without needing a collector."""

    def __init__(self):
        self._lock = threading.Lock()
        self.spans = []       # list of dicts, newest last, capped
        self.counters = {}    # (name, frozenset(labels.items())) -> int
        self.histograms = {}  # (name, frozenset(labels.items())) -> list[float]
        self._cap = 2000

    @contextmanager
    def span(self, name, trace_id=None, span_id=None, **attrs):
        t0 = time.perf_counter()
        parent = _CUR.get()
        rec = {"name": name, "trace_id": trace_id or (parent["trace_id"] if parent else _new_id(16)),
               "span_id": span_id or _new_id(8), "parent_id": parent["span_id"] if parent else None,
               "start": time.time(),
               "attrs": {k: redact(v) if isinstance(v, str) else v for k, v in attrs.items()},
               "ok": True, "error": None}
        token = _CUR.set({"trace_id": rec["trace_id"], "span_id": rec["span_id"]})
        try:
            yield rec
        except Exception as e:
            rec["ok"], rec["error"] = False, f"{type(e).__name__}: {redact(str(e))}"
            raise
        finally:
            _CUR.reset(token)
            rec["ms"] = round((time.perf_counter() - t0) * 1000, 2)
            with self._lock:
                self.spans.append(rec)
                if len(self.spans) > self._cap:
                    self.spans = self.spans[-self._cap:]

    def counter(self, name, value=1, **labels):
        key = (name, frozenset(labels.items()))
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + value

    def histogram(self, name, value, **labels):
        key = (name, frozenset(labels.items()))
        with self._lock:
            self.histograms.setdefault(key, []).append(value)
            if len(self.histograms[key]) > self._cap:
                self.histograms[key] = self.histograms[key][-self._cap:]

    def summary(self):
        """A JSON-safe snapshot: counters as-is, histograms reduced to
        count/avg/p95/max. This is the 'measures' deliverable."""
        def pct(vals, p):
            if not vals:
                return None
            s = sorted(vals)
            return s[int(max(0, min(len(s) - 1, round(p * (len(s) - 1)))))]
        with self._lock:
            counters = {f"{n}{dict(l) and ' ' + str(dict(l)) or ''}": v for (n, l), v in self.counters.items()}
            hist = {}
            for (n, l), vals in self.histograms.items():
                label = f"{n}{dict(l) and ' ' + str(dict(l)) or ''}"
                hist[label] = {"count": len(vals), "avg_ms": round(sum(vals) / len(vals), 2) if vals else None,
                                "p95_ms": pct(vals, 0.95), "max_ms": max(vals) if vals else None}
            return {"counters": counters, "histograms": hist, "recent_spans": self.spans[-50:]}

    def snapshot_spans(self):
        with self._lock:
            return list(self.spans)

    def clear(self):
        with self._lock:
            self.spans, self.counters, self.histograms = [], {}, {}

    def raw_counters(self):
        with self._lock:
            return [(n, dict(l), v) for (n, l), v in self.counters.items()]

    def raw_histograms(self):
        with self._lock:
            return [(n, dict(l), list(v)) for (n, l), v in self.histograms.items()]


_fallback = _Recorder()


def reset():
    """Clear the in-process recorder (tests, or an operator starting a clean measurement window)."""
    _fallback.clear()
_otel = None  # set up lazily, only if FURY_OTEL=on and the SDK imports cleanly


def _try_init_otel():
    global _otel
    if os.getenv("FURY_OTEL", "off").lower() != "on":
        return None
    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            span_exporter, metric_exporter = OTLPSpanExporter(endpoint=endpoint), OTLPMetricExporter(endpoint=endpoint)
        else:
            span_exporter, metric_exporter = ConsoleSpanExporter(), ConsoleMetricExporter()

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        meter_provider = MeterProvider(metric_readers=[PeriodicExportingMetricReader(metric_exporter)])
        metrics.set_meter_provider(meter_provider)
        tracer = trace.get_tracer("fury")
        meter = metrics.get_meter("fury")
        _otel = {
            "tracer": tracer,
            "counters": {},
            "histograms": {},
            "meter": meter,
        }
        return _otel
    except Exception:
        return None  # any import/setup failure: silently use the fallback recorder


_otel = _try_init_otel()


@contextmanager
def span(name, **attrs):
    """with telemetry.span('agent.hop', skill=skill, to=name) as s: ...
    Always also feeds the local fallback recorder, even when real OTel is
    active and shipping spans elsewhere -- /api/metrics, /api/traces and the
    eval harness read the local view regardless of backend. When OTel is
    active the local record reuses OTel's trace/span ids, so a trace opened
    in the dashboard can be found in Jaeger/Tempo by the same id."""
    if _otel:
        with _otel["tracer"].start_as_current_span(name) as s:
            for k, v in attrs.items():
                s.set_attribute(k, redact(v) if isinstance(v, str) else v)
            ctx = s.get_span_context()
            ids = {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
            with _fallback.span(name, **ids, **attrs) as rec:
                try:
                    yield rec
                    s.set_attribute("ok", rec["ok"])
                    for k, v in rec["attrs"].items():
                        if isinstance(v, (str, bool, int, float)):
                            s.set_attribute(k, v)
                except Exception as e:
                    s.set_attribute("ok", False)
                    s.set_attribute("error", redact(str(e)))
                    raise
        return
    with _fallback.span(name, **attrs) as rec:
        yield rec


def counter(name, value=1, **labels):
    if _otel:
        key = name
        c = _otel["counters"].get(key) or _otel["counters"].setdefault(key, _otel["meter"].create_counter(name))
        c.add(value, {k: redact(v) if isinstance(v, str) else v for k, v in labels.items()})
    _fallback.counter(name, value, **labels)


def histogram(name, value, **labels):
    if _otel:
        key = name
        h = _otel["histograms"].get(key) or _otel["histograms"].setdefault(key, _otel["meter"].create_histogram(name))
        h.record(value, {k: redact(v) if isinstance(v, str) else v for k, v in labels.items()})
    _fallback.histogram(name, value, **labels)


def summary():
    """Always returns the in-process view: a JSON-safe snapshot of counters,
    histograms (count/avg/p95/max) and the last 50 spans. This is what
    /api/metrics and the eval harness read, regardless of backend."""
    return _fallback.summary()


def backend():
    return "opentelemetry" if _otel else "builtin"


# ------------------------------------------------------------ read models
# Everything below feeds the dashboard. It reads the local recorder only and
# returns metadata (names, ids, durations, enums), never raw prompt text.
def traces(limit=30, session=None):
    """One row per trace, newest first. `session` filters to one chat conversation (see webapp.session_ref)."""
    by = {}
    for sp in _fallback.snapshot_spans():
        by.setdefault(sp["trace_id"], []).append(sp)
    rows = []
    for tid, sps in by.items():
        t0 = min(x["start"] for x in sps)
        t1 = max(x["start"] + x["ms"] / 1000 for x in sps)
        root = next((x for x in sps if x["parent_id"] is None), min(sps, key=lambda x: x["start"]))
        run_sp = next((x for x in sps if x["name"] == "run"), None)
        failed = [x for x in sps if not x["ok"] or x["attrs"].get("state") == "failed"]
        rows.append({"trace_id": tid, "root": root["name"], "start": t0, "ms": round((t1 - t0) * 1000, 1),
                     "spans": len(sps), "errors": len(failed),
                     "source": (run_sp or root)["attrs"].get("source"),
                     "label": (run_sp or root)["attrs"].get("label"),
                     "intent": root["attrs"].get("intent"), "session": root["attrs"].get("session"),
                     "outcome": (run_sp["attrs"].get("outcome") if run_sp else None)})
    if session: rows = [r for r in rows if r["session"] == session]
    rows.sort(key=lambda r: r["start"], reverse=True)
    return rows[:limit]


def trace(trace_id):
    """All spans of one trace, ordered by start, with offsets for a waterfall."""
    sps = sorted((x for x in _fallback.snapshot_spans() if x["trace_id"] == trace_id), key=lambda x: x["start"])
    if not sps:
        return None
    t0 = sps[0]["start"]
    depth = {}
    out = []
    for x in sps:
        d = 0 if x["parent_id"] is None else depth.get(x["parent_id"], 0) + 1
        depth[x["span_id"]] = d
        out.append({**x, "offset_ms": round((x["start"] - t0) * 1000, 2), "depth": d})
    total = max(x["offset_ms"] + x["ms"] for x in out)
    return {"trace_id": trace_id, "total_ms": round(total, 2), "spans": out}


def _sum(name, **want):
    return sum(v for n, l, v in _fallback.raw_counters() if n == name and all(l.get(k) == w for k, w in want.items()))


def _by(name, label):
    d = {}
    for n, l, v in _fallback.raw_counters():
        if n == name:
            d[l.get(label, "?")] = d.get(l.get(label, "?"), 0) + v
    return d


def _p95(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[int(round(0.95 * (len(s) - 1)))]


# Thresholds are deliberately plain constants: an operator can read them and
# argue with them. Each signal names an owner and the action to take.
SIGNALS_CFG = {"llm_fallback_rate": 0.25, "escalation_rate": 0.5, "min_samples": 4, "hop_p95_ms": 3000, "tool_error_rate": 0.1}


def signals():
    """Operator-facing health signals. Each one is something a person acts on."""
    out = []
    # LLM reliability
    by = _by("llm.classify.count", "result")
    total = sum(by.values())
    live = total - by.get("off", 0)
    bad = sum(v for k, v in by.items() if k not in ("off", "ok"))
    if live >= SIGNALS_CFG["min_samples"]:
        rate = bad / live
        out.append({"id": "llm_fallback", "title": "Model classification failing", "level": "act" if rate > SIGNALS_CFG["llm_fallback_rate"] else "ok",
                    "value": f"{round(rate * 100)}% of {live} live calls fell back", "threshold": f"> {round(SIGNALS_CFG['llm_fallback_rate'] * 100)}%",
                    "owner": "Platform on-call", "action": "Check NVIDIA_API_KEY, base URL and network egress. Chat still works on keyword rules meanwhile."})
    else:
        out.append({"id": "llm_fallback", "title": "Model classification failing", "level": "idle", "value": "model off or too few calls",
                    "threshold": f"needs {SIGNALS_CFG['min_samples']} live calls", "owner": "Platform on-call", "action": "Turn FURY_LLM on to track this."})
    # Outcomes
    oc = _by("triage.outcome", "outcome")
    n = sum(oc.values())
    esc = oc.get("escalated", 0)
    if n >= SIGNALS_CFG["min_samples"]:
        rate = esc / n
        out.append({"id": "escalation", "title": "Too many incidents escalating", "level": "act" if rate > SIGNALS_CFG["escalation_rate"] else "ok",
                    "value": f"{esc} of {n} escalated", "threshold": f"> {round(SIGNALS_CFG['escalation_rate'] * 100)}%",
                    "owner": "SRE lead", "action": "Open the escalated traces, find the common step that failed, then fix the runbook or tool."})
    else:
        out.append({"id": "escalation", "title": "Too many incidents escalating", "level": "idle", "value": f"{n} outcomes so far",
                    "threshold": f"needs {SIGNALS_CFG['min_samples']} outcomes", "owner": "SRE lead", "action": "Run a few incidents to start tracking."})
    # Approvals that went nowhere
    spans = _fallback.snapshot_spans()
    ap = [x for x in spans if x["name"] == "approval.wait"]
    stuck = [x for x in ap if x["attrs"].get("decision") in ("timeout", "rejected")]
    out.append({"id": "approvals", "title": "Approvals not granted", "level": "act" if len(stuck) >= 2 else ("ok" if ap else "idle"),
                "value": f"{len(stuck)} of {len(ap)} timed out or rejected" if ap else "no approvals yet", "threshold": ">= 2",
                "owner": "SRE Admin", "action": "Check the Slack approvals channel and who is on call to approve."})
    # Slow hops
    slow = []
    for n_, l, vals in _fallback.raw_histograms():
        if n_ == "agent.hop.duration_ms" and len(vals) >= 2 and l.get("to") != "triage":
            p = _p95(vals)
            if p and p > SIGNALS_CFG["hop_p95_ms"]:
                slow.append(f"{l.get('to')}.{l.get('skill')} p95 {round(p)} ms")
    out.append({"id": "slow_hops", "title": "Slow agent step", "level": "act" if slow else "ok", "value": "; ".join(slow) or "all steps under limit",
                "threshold": f"p95 > {SIGNALS_CFG['hop_p95_ms']} ms", "owner": "Service owner of that step", "action": "Open a recent trace and look at the widest span."})
    # Tool failures
    tools = [x for x in spans if x["name"] == "tool.call"]
    tf = [x for x in tools if not x["ok"]]
    if tools:
        rate = len(tf) / len(tools)
        out.append({"id": "tools", "title": "Tool calls failing", "level": "act" if rate > SIGNALS_CFG["tool_error_rate"] else "ok",
                    "value": f"{len(tf)} of {len(tools)} failed", "threshold": f"> {round(SIGNALS_CFG['tool_error_rate'] * 100)}%",
                    "owner": "Service owner", "action": "Check AWS access and the target server, then re-run the failing check."})
    return out


def kpis():
    """Headline numbers for the top of the dashboard."""
    spans = _fallback.snapshot_spans()
    oc = _by("triage.outcome", "outcome")
    runs = [x for x in spans if x["name"] == "run"]
    ms = [x["ms"] for x in runs]
    cls = _by("llm.classify.count", "result")
    live = sum(v for k, v in cls.items() if k != "off")
    tin = sum(x["attrs"].get("tokens_in") or 0 for x in spans if x["name"] == "llm.classify")
    tout = sum(x["attrs"].get("tokens_out") or 0 for x in spans if x["name"] == "llm.classify")
    resolved, total = oc.get("resolved", 0), sum(oc.values())
    return {"runs": len(runs), "outcomes": oc, "resolve_rate": round(resolved / total, 3) if total else None,
            "run_p95_ms": _p95(ms), "run_avg_ms": round(sum(ms) / len(ms), 1) if ms else None,
            "tool_calls": sum(1 for x in spans if x["name"] == "tool.call"),
            "llm_calls": live, "llm_ok": cls.get("ok", 0), "llm_results": cls, "tokens_in": tin, "tokens_out": tout,
            "errors": sum(1 for x in spans if not x["ok"])}
