"""Web UI + API. Engineers chat here; alarms from CloudWatch also show up as runs. Approvals are NOT done here:
they go to the SRE Admin on Slack (a different person), the UI only shows that one is pending."""
import hashlib, json, os, re, threading, time, uuid
from pathlib import Path
from flask import Flask, g, jsonify, request, send_from_directory
from . import audit as AUD, retrieval, runbooks as RB, slo
from . import llm as LLM
from . import telemetry
from .agents import REPORT_KEYWORDS

STEP_TEXT = {"match_runbook": "Looked for a matching runbook", "run_checks": "Ran read-only checks on the server",
             "open_incident": "Opened a ServiceNow incident", "add_note": None, "request_approval": "Asked the SRE Admin for approval on Slack",
             "remediate": "Ran the approved fix", "resolve": "Resolved the incident in ServiceNow", "assign": "Assigned the incident to the L2 team",
             "notify_escalation": "Notified the team on Slack"}

def final_message(out):
    t, o = out.get("ticket"), out.get("outcome")
    if o == "resolved": return f"Fixed. {t} is resolved after SRE Admin approval, and I re-checked that the problem is gone."
    if o == "escalated":
        return (f"I could not fix this myself: {out.get('reason')}. " + (f"{t} is assigned to {out.get('team')}" if t else "No ticket could be created") + " and the team was notified on Slack.")
    if o == "false_alarm": return "I checked the server and it looks healthy now, so I did not open a ticket."
    if o == "duplicate": return f"This is already being tracked in {t}. I added a note to it."
    if o == "reply": return out["message"]
    return str(out)


MAX_MESSAGE_CHARS = 1000   # same limit the UI enforces; the server must not rely on the browser

def _facts_safe(text):
    return telemetry.redact(text, 3000) if text else ""

def grounded_reply(ctx, intent, text):
    """Phrase an answer with the model from deterministic facts; on any failure return the deterministic text itself.
    Privacy: only runbook text, ticket numbers, states and team names are sent. Work notes and server evidence never are."""
    if intent == "howto":
        base = howto_reply(ctx, text)
        if base.startswith("I do not have a runbook"):
            kb = kb_answer(ctx, text)   # no approved runbook: fall back to the organisation's ServiceNow knowledge articles (informational only)
            if kb: return kb
            return LLM.compose("howto_no_runbook", "FURY has no runbook that covers this question.", text) or base
        head, sep, tail = base.partition("\nSource:")
        composed = LLM.compose("howto", "Runbook (authoritative steps):\n" + head, text)
        return (composed + "\n\nSource:" + tail) if composed else base
    if intent == "help":
        return LLM.compose("help", HELP_TEXT, text) or HELP_TEXT
    if intent == "incident_status":
        base = incident_status(ctx, text)
        if base.startswith("I could not"): return base
        first, sep, notes = base.partition("\nLatest notes:")
        composed = LLM.compose("incident_status", _facts_safe(first), text)
        return (composed + ("\n\nLatest notes:" + notes if notes else "")) if composed else base
    base = incident_list(ctx)
    if not LLM.enabled(): return base
    lead = LLM.compose("incident_list", _facts_safe(base), text)
    if base.startswith(("I could not", "There are no open")): return lead or base
    return (lead + "\n\n" + base) if lead else base

def kb_answer(ctx, text):
    """Answer a how/why question from ingested ServiceNow knowledge articles. Always labelled informational and cited.
    Returns None when no article supports the question (the caller then says it has no information)."""
    docs = getattr(getattr(ctx, "kb", None), "docs", None)
    if not docs: return None
    hits = retrieval.search(docs, text, k=3, vectors=getattr(ctx, "vectors", None))
    if not hits: return None
    cites = sorted({h["runbook"] for h in hits})
    facts = "Knowledge articles from ServiceNow (INFORMATIONAL guidance, not an approved fix procedure; FURY will not run anything from them):\n" + \
            "\n".join(f"[{h['runbook']}] {h['title']}: {h['text']}" for h in hits)
    composed = LLM.compose("howto_kb", _facts_safe(facts), text)
    body = composed or f"{hits[0]['title']}: {hits[0]['text']}"
    return body + "\n\nSource: ServiceNow knowledge " + ", ".join(cites) + " (informational, not an approved fix). Any change on the server still needs a runbook and the SRE Admin's approval."

def narrate_outcome(out):
    """One or two natural sentences for a finished run, built only from the structured outcome (never raw evidence)."""
    fixed = final_message(out)
    o = out.get("outcome")
    if o not in ("resolved", "escalated", "false_alarm", "duplicate"): return fixed
    facts = (f"outcome: {o}\nticket: {out.get('ticket') or 'none'}\nreason: {out.get('reason') or 'n/a'}\nteam: {out.get('team') or 'n/a'}\n"
             "meaning: resolved = the SRE Admin approved the fix, FURY ran it and re-checked the server, which is healthy; "
             "escalated = FURY could not fix it itself, handed the ticket to the L2 team and notified them on Slack; "
             "false_alarm = the checks found the server healthy so no ticket was opened; "
             "duplicate = an open incident already tracks this and a note was added to it.")
    return LLM.compose("run_outcome", _facts_safe(facts), "") or fixed


# ---------------- Chat intent layer: only real, confirmed SRE problems may reach triage ----------------
INC_RE = re.compile(r"\bINC\d{6,9}\b", re.I)
PROBLEM = {"down", "full", "high", "slow", "error", "errors", "fail", "failed", "failing", "failure", "stopped", "crash", "crashed", "outage",
           "unreachable", "timeout", "timeouts", "spike", "spiking", "broken", "hung", "stuck", "unavailable", "offline", "leak", "leaking",
           "maxed", "exhausted", "runaway", "frozen", "502", "503", "500", "check", "healthy", "status", "issue", "issues", "problem", "problems", "wrong", "ok", "okay", "fine"}
PROBLEM_PHRASES = ("not working", "out of space", "not responding", "not loading", "cannot access", "can't access", "not reachable")
GREETINGS = {"hi", "hii", "hello", "hey", "yo", "thanks", "thank you", "thx", "ok", "okay", "bye", "good morning", "good afternoon", "good evening"}
HELP = ("what can you do", "who are you", "what are you", "help", "what do you do", "how do you work")
HOWTO = re.compile(r"^(how\b|explain\b|why\b|steps?\b|runbook\b|what (is|are|does)\b)")
CLOSED = {"6", "7", "8", "resolved", "closed", "canceled", "cancelled"}
STATE = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed"}

HELP_TEXT = ("I am FURY, the SRE assistant for this server. I can:\n"
             "- investigate a problem you report (website down, disk full, CPU high, memory) and open a ServiceNow incident only if my checks confirm it\n"
             "- tell you the status and latest notes of an incident, for example \"status of INC0010020\"\n"
             "- list open incidents, for example \"show open incidents\"\n"
             "- explain how we fix something, for example \"how do you fix a full disk?\"\n"
             "Any change on the server needs the SRE Admin to approve on Slack first.")
OFF_TOPIC = "I can only help with server operations: reporting a problem, incident status, or how we fix something. Try \"the website is down\" or \"status of INC0010020\"."
CLARIFY = "Which area is affected: the website or a service, disk space, CPU, or memory? Tell me what you see and I will check it."
REPORT_DISABLED = "Incident reporting is disabled in read-only mode. You can still check incident status, list incidents, or ask how-to questions."
SESSION_BUSY = "Your previous request is still running. Please wait."
SESSION_IDLE_TTL = 30 * 60

def metric_of(text):
    words = set(RB.tokens(text)) | set(text.lower().replace("?", " ").replace(",", " ").split())
    return next((m for m, kw in REPORT_KEYWORDS if words & kw), None)

def classify(text):
    t = " ".join(text.lower().split()); t_plain = t.rstrip(".!?")
    words = set(re.split(r"[^a-z0-9:']+", t))
    if INC_RE.search(text): return "incident_status"
    if re.search(r"\b(open|active|recent|latest|my|all)\b.*\b(incidents?|tickets?)\b|\b(list|show)\b.*\b(incidents?|tickets?)\b", t): return "incident_list"
    if t_plain in GREETINGS: return "greeting"
    if any(h in t for h in HELP) and not metric_of(t): return "help"
    metric = metric_of(t); signal = bool(words & PROBLEM) or any(p in t for p in PROBLEM_PHRASES)
    if HOWTO.match(t) and not re.search(r"\bis wrong\b|\bwrong with\b", t): return "howto"
    if metric and signal: return "report"
    if metric or signal: return "clarify"
    return "off_topic"

def session_ref(session_id):
    """Short one-way reference for a chat session: correlates runs and traces from one conversation
    without the raw client-side session id appearing in telemetry."""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:10]

class _ChatSessionPolicy:
    def __init__(self, idle_ttl=SESSION_IDLE_TTL, clock=time.monotonic):
        self.idle_ttl, self._clock = idle_ttl, clock
        self._last_activity, self._in_flight = {}, set()
        self._lock = threading.Lock()

    def begin(self, session_id, reset_context):
        with self._lock:
            if session_id in self._in_flight: return False
            now = self._clock()
            last = self._last_activity.get(session_id)
            if last is None or now - last > self.idle_ttl:
                reset_context(session_id)
            self._last_activity[session_id] = now
            self._in_flight.add(session_id)
            return True

    def finish(self, session_id):
        with self._lock:
            self._in_flight.discard(session_id)
            self._last_activity[session_id] = self._clock()

def incident_status(ctx, text):
    n = INC_RE.search(text).group(0).upper()
    try:
        row = next((r for r in ctx.snow.list_recent(ctx.instance, 50) if r["number"].upper() == n), None)
        if not row: return f"I could not find {n} among the incidents FURY opened for this server."
        notes = ctx.snow.get_notes(n)[-2:]
    except Exception:
        return "I could not reach ServiceNow just now. Please try again in a moment."
    st = STATE.get(str(row["state"]), row["state"])
    owner = row.get("assigned_to") or ""; team = row.get("assignment_group") or ""
    out = f"{n}: {row['short_description']}\nState: {st}. " + (f"Owner: {owner}. " if owner else "") + (f"Team: {team}." if team else ("" if owner else "Not assigned yet."))
    if notes: out += "\nLatest notes:\n" + "\n".join("- " + x["text"][:300] for x in notes)
    return out

def incident_list(ctx):
    try: rows = [r for r in ctx.snow.list_recent(ctx.instance, 50) if str(r["state"]).lower() not in CLOSED]
    except Exception: return "I could not reach ServiceNow just now. Please try again in a moment."
    if not rows: return "There are no open incidents opened by FURY for this server."
    return f"{len(rows)} open incident(s):\n" + "\n".join(f"- {r['number']}: {r['short_description']} ({STATE.get(str(r['state']), r['state'])}, {r.get('assignment_group') or 'unassigned'})" for r in rows[:5])

def howto_reply(ctx, text):
    rbs = ctx.bus.agents["knowledge"].rbs
    rb, _ = RB.match(rbs, text)
    if not rb:   # fall back to governed retrieval (fuzzy + lexical); it abstains when nothing supports the question
        hits = retrieval.search(rbs, text, k=1, vectors=getattr(ctx, "vectors", None))
        rb = next((r for r in rbs if hits and r.id == hits[0]["runbook"]), None)
    if not rb: return "I do not have a runbook for that. If something is actually broken, describe the symptom and I will check it."
    steps = "\n".join(f"{s.n}. {s.text}" for s in rb.steps)
    return f"Runbook {rb.citation()}: {rb.title}\n{steps}\nSource: {rb.source} (owner {rb.owner_team}, content {rb.content_hash}).\nI only run the fix after the SRE Admin approves it on Slack."

class Ctx:
    def __init__(self, bus, triage, snow, chat, instance, demo_target=None, store=None, audit=None):
        self.bus, self.triage, self.snow, self.chat, self.instance, self.demo_target = bus, triage, snow, chat, instance, demo_target
        self.runs, self.lock, self.poller, self.store = {}, threading.Lock(), None, store
        from . import kb as KB, vectors as VEC
        self.kb = KB.KnowledgeBase(snow)
        # Semantic search is opt-in (FURY_SEMANTIC=on) until its relevance floor (FURY_EMBED_MIN) has been calibrated against the golden set.
        self.vectors = VEC.VectorIndex() if (LLM.enabled() and os.getenv("FURY_SEMANTIC", "off").lower() == "on") else None
        self.audit = audit or AUD.Audit(os.getenv("FURY_AUDIT_LOG") or None)
        from .voice import VoiceSessionBroker
        from .mcp import McpToolRegistry, AutonomyTier
        self.voice = VoiceSessionBroker()
        self.mcp = McpToolRegistry()
        self._init_mcp()
        if store:
            store.recover_interrupted(int(os.getenv("FURY_RESUME_WINDOW_SECONDS", "1800")), int(os.getenv("FURY_RESUME_MAX", "1")))
            store.prune(int(os.getenv("FURY_RETENTION_DAYS", "90"))); self.runs.update(store.load_runs())
    def _init_mcp(self):
        from .mcp import AutonomyTier
        self.mcp.register_tool(
            "check_disk", "Check disk usage on drive",
            {"type": "object", "properties": {"drive": {"type": "string"}}, "required": ["drive"]},
            lambda drive="F:": getattr(self.demo_target, "check_disk", lambda **k: {"drive": drive, "healthy": True})(drive=drive) if self.demo_target else {"drive": drive, "healthy": True},
            AutonomyTier.TIER_1_READ_ONLY
        )
        self.mcp.register_tool(
            "check_cpu", "Check CPU load and top process",
            {"type": "object", "properties": {}},
            lambda: getattr(self.demo_target, "check_cpu", lambda **k: {"healthy": True})() if self.demo_target else {"healthy": True},
            AutonomyTier.TIER_1_READ_ONLY
        )
        self.mcp.register_tool(
            "check_service", "Check Windows service state",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            lambda name="W3SVC": getattr(self.demo_target, "check_service", lambda **k: {"name": name, "healthy": True})(name=name) if self.demo_target else {"name": name, "healthy": True},
            AutonomyTier.TIER_1_READ_ONLY
        )
        self.mcp.register_tool(
            "restart_service", "Restart Windows service (Tier 3 approval required)",
            {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            lambda target="W3SVC": getattr(self.demo_target, "act", lambda a, t: {"restarted": t})("restart_service", target) if self.demo_target else {"restarted": target},
            AutonomyTier.TIER_3_APPROVAL_REQUIRED
        )
    def save(self, run):
        if self.store: self.store.save_run(run)
    def submit(self, skill, data, source, label, on_complete=None):
        rid = uuid.uuid4().hex[:8]
        # "input" and "cp" (checkpoint) exist only while the run is active so a restart can resume it; they are dropped when it ends.
        run = self.runs[rid] = {"id": rid, "label": label, "source": source, "state": "queued", "start": None, "end": None, "out": None, "at": time.strftime("%H:%M:%S"),
                                "ts": time.time(), "resumes": 0, "input": {"skill": skill, "data": data, "source": source}, "cp": {}}
        return self._start(run, skill, data, source, on_complete)
    def resume_interrupted(self):
        """Continue runs that a restart interrupted, from their saved checkpoint. Call once, after the integrations are up."""
        ids = []
        for run in [r for r in self.runs.values() if r.get("state") == "interrupted"]:
            inp = run.get("input") or {}
            run.update(resumes=int(run.get("resumes", 0)) + 1, state="queued", start=None, end=None)
            data = dict(inp.get("data") or {}); data["_resume"] = run.get("cp") or {}
            self.audit.record("system", "run.resume", "started", run=run["id"], ticket=(run.get("cp") or {}).get("ticket"), fix_already_run=bool((run.get("cp") or {}).get("acted")))
            telemetry.counter("run.resumed")
            self._start(run, inp.get("skill", "handle_alert"), data, inp.get("source", "cloudwatch"), None); ids.append(run["id"])
        return ids
    def _start(self, run, skill, data, source, on_complete):
        rid, label = run["id"], run["label"]
        self.save(run)
        snap = telemetry.capture()   # the worker thread continues the caller's trace (chat request or alarm)
        def work():
            try:
                with telemetry.attach(snap), telemetry.span("run", run_id=rid, source=source, skill=skill, label=label) as sp:
                    run["trace_id"] = sp["trace_id"]
                    with self.lock:
                        def checkpoint(**kw): run.setdefault("cp", {}).update(kw); self.save(run)
                        self.triage.checkpoint = checkpoint
                        run.update(state="running", start=len(self.bus.trace)); self.save(run)
                        try: t = self.bus.send(source, skill, data, to="triage")
                        finally: self.triage.checkpoint = lambda **kw: None
                        run.update(state="done" if t.state == "completed" else "error", end=len(self.bus.trace),
                                   out=t.output if t.state == "completed" else {"outcome": "error", "reason": t.error})
                        run.pop("input", None); run.pop("cp", None)
                        o = (run["out"] or {}).get("outcome")
                        telemetry.set_attr(sp, "outcome", o); telemetry.set_attr(sp, "ticket", (run["out"] or {}).get("ticket"))
                        if run["state"] == "error": sp["ok"], sp["error"] = False, telemetry.redact(t.error, 120)
                        self.save(run)
            finally:
                if on_complete: on_complete()
        threading.Thread(target=work, daemon=True).start(); return rid
    def answer(self, label, message):
        """A chat reply that needs no investigation: no agents, no ticket, not listed under recent runs."""
        rid = uuid.uuid4().hex[:8]
        self.runs[rid] = {"id": rid, "label": label, "source": "chat", "state": "done", "start": None, "end": None,
                          "out": {"outcome": "reply", "message": message}, "at": time.strftime("%H:%M:%S")}
        self.save(self.runs[rid])
        return rid
    def _message(self, run):
        out = run["out"]
        if not out: return None
        if out.get("outcome") == "reply": return out["message"]
        if "_narration" not in run:
            run["_narration"] = None                          # claim the slot so concurrent polls do not each call the model
            run["_narration"] = narrate_outcome(out)
        return run["_narration"] or final_message(out)
    def view(self, run):
        s, e = run["start"], run["end"]
        steps = [] if s is None else [{"text": STEP_TEXT[x["skill"]], "state": x["state"]} for x in self.bus.trace[s:e] if STEP_TEXT.get(x["skill"])]
        d = {k: run[k] for k in ("id", "label", "source", "state", "at")}; d["trace_id"] = run.get("trace_id"); d["session"] = run.get("session")
        d.update(steps=steps, ticket=(run["out"] or {}).get("ticket") or (self.triage.ticket if run["state"] == "running" else None),
                 outcome=(run["out"] or {}).get("outcome"), message=self._message(run), waiting_approval=bool(self.chat.pending()) and run["state"] == "running")
        return d

def create_app(ctx, demo=False, auth=None):
    app = Flask(__name__, static_folder=None); ui = Path(__file__).resolve().parent.parent / "ui"
    session_policy = _ChatSessionPolicy()
    if auth is not None:
        auth.audit = auth.audit or ctx.audit
        @app.before_request
        def _gate():
            ok, status, user, msg = auth.check(request.method, request.path, request.headers.get("Authorization"), request.remote_addr)
            g.user = user["name"] if user else None
            if not ok: return jsonify(error=msg), status
    @app.after_request
    def _security_headers(resp):
        """Defence in depth for the browser: no framing, no MIME sniffing, same-origin only, API responses never cached."""
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/"): resp.headers["Cache-Control"] = "no-store"
        return resp
    def actor(session_id): return getattr(g, "user", None) or session_ref(session_id)
    def link(rid, root, session_id):
        run = ctx.runs[rid]; run["trace_id"] = root["trace_id"]; run["session"] = session_ref(session_id); ctx.save(run)
    @app.get("/")
    def index(): return send_from_directory(ui, "index.html")
    @app.post("/api/chat")
    def chat():
        body = request.get_json(silent=True)
        msg = body.get("message") if isinstance(body, dict) else None
        if not isinstance(msg, str): return jsonify(error="send a JSON object like {\"message\": \"...\"}"), 400
        text = msg.strip()
        if not text: return jsonify(error="empty message"), 400
        if len(text) > MAX_MESSAGE_CHARS: return jsonify(error=f"message too long (limit {MAX_MESSAGE_CHARS} characters)"), 413
        session_id = (request.headers.get("X-Fury-Session") or "default")[:80]
        if not session_policy.begin(session_id, LLM.SESSIONS.reset):
            return jsonify(run=ctx.answer(text[:60], SESSION_BUSY))
        handed_off = False
        try:
            # One root span per chat request. Only shapes are recorded (length, intent), never the message itself.
            with telemetry.span("chat.request", source="chat", text_len=len(text), session=session_ref(session_id)) as root:
                verdict = LLM.classify(text, session_id=session_id)  # None if FURY_LLM is off, or on any failure: falls back to rules below
                intent = verdict["intent"] if verdict else classify(text)
                telemetry.set_attr(root, "intent", intent); telemetry.set_attr(root, "classifier", "llm" if verdict else "rules")
                telemetry.counter("chat.intent", intent=intent, classifier="llm" if verdict else "rules")
                if intent == "report":
                    try:
                        chat_mode = os.getenv("FURY_CHAT_MODE", "readonly").strip().lower()
                    except Exception as exc:
                        telemetry.counter("chat.mode_check.error", error=type(exc).__name__)
                        chat_mode = "readonly"
                    if chat_mode != "full":
                        ctx.audit.record(actor(session_id), "chat.report", "denied", reason="read-only mode")
                        rid = ctx.answer(text[:60], REPORT_DISABLED)
                        link(rid, root, session_id)
                        return jsonify(run=rid)
                    ctx.audit.record(actor(session_id), "chat.report", "allowed", text_len=len(text))
                    rid = ctx.submit("handle_report", {"text": text, "instance": ctx.instance}, "chat", text[:60],
                                     on_complete=lambda: session_policy.finish(session_id))
                    handed_off = True
                    link(rid, root, session_id)
                    return jsonify(run=rid)
                if intent in ("howto", "incident_status", "incident_list", "help"):
                    reply = grounded_reply(ctx, intent, text)   # facts come from FURY's own code; the model only phrases them (or falls back to fixed text)
                elif verdict and verdict.get("reply") and intent in ("greeting", "off_topic", "clarify"):
                    reply = verdict["reply"]
                else:
                    reply = {"greeting": "You are welcome." if re.match(r"^(thanks|thank you|thx)", text.strip().lower()) else "Goodbye." if text.strip().lower().startswith("bye") else "Hello. Tell me what is wrong with the server, or ask about an incident.", "help": HELP_TEXT, "clarify": CLARIFY,
                             "off_topic": OFF_TOPIC}.get(intent) or (incident_status(ctx, text) if intent == "incident_status" else incident_list(ctx) if intent == "incident_list" else howto_reply(ctx, text))
                rid = ctx.answer(text[:60], reply)
                link(rid, root, session_id)
                return jsonify(run=rid)
        finally:
            if not handed_off:
                session_policy.finish(session_id)
    @app.post("/api/chat/reset")
    def chat_reset():
        session_id = (request.headers.get("X-Fury-Session") or "default")[:80]
        LLM.SESSIONS.reset(session_id)
        return jsonify(ok=True)
    @app.get("/api/run/<rid>")
    def run(rid): return (jsonify(ctx.view(ctx.runs[rid])) if rid in ctx.runs else (jsonify(error="unknown run"), 404))
    @app.get("/api/runs")
    def runs(): return jsonify([ctx.view(r) for r in sorted(ctx.runs.values(), key=lambda r: r["at"], reverse=True) if (r["out"] or {}).get("outcome") != "reply"][:20])
    @app.get("/api/incidents")
    def incidents():
        try: return jsonify(ok=True, incidents=ctx.snow.list_recent(ctx.instance))
        except Exception as e: return jsonify(ok=False, error=str(e), incidents=[])
    @app.get("/api/incident/<n>")
    def incident(n):
        try: return jsonify(ok=True, notes=ctx.snow.get_notes(n))
        except Exception as e: return jsonify(ok=False, error=str(e), notes=[])
    @app.get("/api/audit")
    def audit_log():
        """Recent security-relevant events plus the chain head (compare against an externally stored head to detect tampering)."""
        return jsonify(head=ctx.audit.head, events=ctx.audit.recent(100))
    @app.get("/api/obs/slo")
    def obs_slo(): return jsonify(slo.evaluate())
    @app.get("/api/kb")
    def kb_status(): return jsonify(count=len(ctx.kb.docs), last_ok=ctx.kb.last_ok, error=ctx.kb.last_error, articles=[{"id": d.id, "title": d.title, "version": d.version} for d in ctx.kb.docs])
    @app.post("/api/kb/refresh")
    def kb_refresh():
        n = ctx.kb.refresh(); ctx.audit.record(actor("kb"), "kb.refresh", "ok" if not ctx.kb.last_error else "error", count=n)
        return jsonify(count=n, error=ctx.kb.last_error)
    @app.get("/api/health")
    def health():
        """Per-integration health: ok / degraded / open (circuit breaker). Unwrapped (fake or direct) integrations report 'unmonitored'."""
        out = []
        for label, comp in (("servicenow", ctx.snow), ("chat", ctx.chat), ("server", ctx.demo_target)):
            h = getattr(comp, "health", None) if comp is not None else None
            out.append(h() if callable(h) else {"name": label, "state": "unmonitored"})
        worst = "open" if any(x["state"] == "open" for x in out) else "degraded" if any(x["state"] == "degraded" for x in out) else "ok"
        return jsonify(status=worst, integrations=out, store=bool(ctx.store))
    @app.get("/api/approvals")
    def approvals(): return jsonify(ctx.chat.pending())
    @app.get("/api/approvals/history")
    def approvals_history():
        """Durable audit trail of every remediation request and its outcome (needs FURY_DB)."""
        return jsonify(enabled=bool(ctx.store), history=ctx.store.approvals_history() if ctx.store else [])
    @app.get("/api/status")
    def status():
        return jsonify(demo=demo, poller_error=getattr(ctx.poller, "last_error", ""), agents=sorted(ctx.bus.agents), instance=ctx.instance)
    @app.get("/api/metrics")
    def metrics():
        """Measures: agent hop latency/counts, triage outcomes, and LLM classify
        success/fallback breakdown, all already redacted at the point of capture.
        Backend tells you whether this is real OpenTelemetry (FURY_OTEL=on + SDK
        installed) or the built-in in-process recorder."""
        return jsonify(backend=telemetry.backend(), **telemetry.summary())
    @app.get("/api/obs/overview")
    def obs_overview():
        """Everything the Observability tab draws, in one call so it never half-renders."""
        return jsonify(backend=telemetry.backend(), otel_endpoint=bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")), llm_on=os.getenv("FURY_LLM", "off").lower() == "on",
                       kpis=telemetry.kpis(), signals=telemetry.signals(), traces=telemetry.traces(25), metrics=telemetry.summary(),
                       retention=telemetry.retention(),redaction=os.getenv("FURY_TELEMETRY_RAW", "off").lower() != "on")
    @app.get("/api/obs/session/<ref>")
    def obs_session(ref):
        """Everything one conversation did: its runs and the traces they produced (metadata only)."""
        runs = [ctx.view(r) for r in sorted(ctx.runs.values(), key=lambda r: r["at"]) if r.get("session") == ref]
        return jsonify(session=ref, runs=runs, traces=telemetry.traces(100, session=ref))
    @app.get("/api/obs/trace/<tid>")
    def obs_trace(tid):
        t = telemetry.trace(tid)
        return (jsonify(t) if t else (jsonify(error="unknown trace"), 404))
    @app.get("/api/obs/eval")
    def obs_eval():
        p = Path(__file__).resolve().parent.parent / "reports" / "eval_latest.json"
        if not p.exists(): return jsonify(available=False)
        try: r = json.loads(p.read_text(encoding="utf-8"))
        except ValueError: return jsonify(available=False)
        return jsonify(available=True, at=time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                       scenarios=r["scenarios"], classifier={k: r["classifier"][k] for k in ("accuracy", "n", "pass")}, misses=[x for x in r["classifier"]["rows"] if not x["correct"]],
                       privacy=r["privacy"], backend=r.get("backend"))
    # ---- Voice Foundation (Phase 1) ----
    @app.get("/api/voice/capabilities")
    def voice_capabilities():
        return jsonify(ctx.voice.get_capabilities())

    @app.post("/api/voice/session")
    def voice_session():
        body = request.get_json(silent=True) or {}
        sid = body.get("session_id")
        user = getattr(g, "user", None) or "engineer"
        return jsonify(ctx.voice.create_session(user_id=user, session_id=sid))

    @app.post("/api/voice/turn")
    def voice_turn():
        b = request.get_json(silent=True) or {}
        sid = b.get("session_id", "default-voice")
        u_text = b.get("user_transcript", "")[:300]
        a_text = b.get("agent_response", "")[:300]
        lat = float(b.get("latency_ms", 0.0))
        interrupted = bool(b.get("interrupted", False))
        ctx.voice.record_turn(sid, u_text, a_text, latency_ms=lat, interrupted=interrupted)
        return jsonify(ok=True)

    # ---- MCP & Governance (Phase 5 & 6) ----
    @app.get("/api/mcp/tools")
    def mcp_tools():
        return jsonify(ctx.mcp.list_tools())

    @app.get("/api/governance/tiers")
    def governance_tiers():
        from .policy import DEFAULT_TIERS
        return jsonify(DEFAULT_TIERS)

    if demo:
        from .system import ALERTS
        @app.post("/api/demo/inject/<fault>")
        def inject(fault):
            if fault not in ALERTS: return jsonify(error="unknown fault"), 404
            ctx.demo_target.inject(fault)
            alert = dict(ALERTS[fault], instance=ctx.instance)   # the configured instance, so tickets match the Incidents list filter
            return jsonify(run=ctx.submit("handle_alert", alert, "cloudwatch", alert["description"]))
        @app.post("/api/demo/reset")
        def reset(): ctx.demo_target.reset(); return jsonify(ok=True)
    return app
