import json, time
from .a2a import Agent, Task
from . import approvals, runbooks as RB
from . import telemetry

READ_TOOLS = {"check_disk", "check_cpu", "check_service", "check_memory", "read_events"}
ACTION_CATALOG = {"restart_service": {"W3SVC"}, "clear_temp_files": {"F:\\fury-tmp"}, "kill_process": {"fury-rogue"}}
GENERIC_CHECKS = {"disk": [("check_disk", {"drive": "F:"}), ("read_events", {"log": "System", "last": "20"})],
                  "cpu": [("check_cpu", {})], "memory": [("check_memory", {}), ("read_events", {"log": "System", "last": "20"})],
                  "service": [("check_service", {"name": "W3SVC"}), ("read_events", {"log": "System", "last": "20"})]}
REPORT_KEYWORDS = [("memory", {"memory", "ram", "leak", "oom"}), ("disk", {"disk", "drive", "space", "storage", "volume", "f:"}),
                   ("cpu", {"cpu", "slow", "latency", "load", "sluggish"}), ("service", {"service", "iis", "w3svc", "website", "site", "down", "stopped", "502", "503"})]
SEVERITY = {"critical": ("1", "1"), "major": ("2", "2"), "minor": ("3", "3")}

class AgentError(Exception): pass
def short(res, limit=300): return json.dumps(res, separators=(",", ":"))[:limit]
def ev_line(e): return f"{e['tool']}({', '.join(f'{k}={v}' for k, v in e['params'].items())}) -> {short(e['result'])}"

# ---------------- Knowledge: owns runbooks (RAG) ----------------
class Knowledge(Agent):
    name, description = "knowledge", "Finds the runbook for a symptom, or says there is none"
    def __init__(self, runbook_list): self.rbs = runbook_list; self.skills = {"match_runbook": self.match}
    def match(self, task):
        with telemetry.span("retrieval.runbook", corpus_size=len(self.rbs)) as sp:
            rb, score = RB.match(self.rbs, task.input["query"])
            telemetry.set_attr(sp, "hit", bool(rb)); telemetry.set_attr(sp, "runbook", rb.id if rb else None)
            telemetry.set_attr(sp, "score", score)
            telemetry.counter("retrieval.count", hit=bool(rb))
        return {"runbook": rb.as_dict() if rb else None, "score": score}

# ---------------- Diagnostic: READ-ONLY on the server ----------------
class Diagnostic(Agent):
    name, description = "diagnostic", "Runs read-only checks on the server and returns evidence"
    def __init__(self, readonly_target): self.t = readonly_target; self.skills = {"run_checks": self.run_checks}
    def run_checks(self, task):
        out = []
        for s in task.input["steps"]:
            if s["tool"] not in READ_TOOLS: raise PermissionError(f"diagnostic is read-only; '{s['tool']}' refused")
            with telemetry.span("tool.call", tool=s["tool"], mode="read") as sp:
                res = getattr(self.t, s["tool"])(**s.get("params", {}))
                if isinstance(res, dict) and "healthy" in res: telemetry.set_attr(sp, "healthy", bool(res["healthy"]))
            telemetry.counter("tool.call.count", tool=s["tool"], mode="read")
            out.append({"tool": s["tool"], "params": s.get("params", {}), "result": res})
        return {"evidence": out}

# ---------------- Remediation: the ONLY agent that can change the server ----------------
class Remediation(Agent):
    name, description = "remediation", "Runs catalog actions after a verified human approval"
    def __init__(self, bus, act_target, store=None):
        self.bus, self.t, self.skills, self.done, self.store = bus, act_target, {"remediate": self.remediate}, {}, store
    def _attempts(self, tk, a): return self.store.attempts(tk, a) if self.store else self.done.get((tk, a), 0)
    def remediate(self, task):
        i = task.input; a, tgt, tk = i["action"], i["target"], i["ticket"]
        if a not in ACTION_CATALOG or tgt not in ACTION_CATALOG[a]:
            raise PermissionError(f"action {a}({tgt}) is not in the approved catalog")
        if self._attempts(tk, a) >= 1: raise PermissionError(f"{a} already run once for {tk}; second attempt must go to L2")
        task.state = "input-required"
        ap = self.bus.send(self.name, "request_approval", {"action": a, "target": tgt, "ticket": tk, "summary": i["summary"]})
        if ap.state != "completed": raise PermissionError(f"approval not granted: {ap.error}")
        p, err = approvals.verify(ap.output["token"], a, tgt, tk)
        if err: raise PermissionError(f"approval rejected by remediation: {err}")
        task.state = "working"
        with telemetry.span("tool.call", tool=a, mode="act") as sp:
            result = self.t.act(a, tgt)
        telemetry.counter("tool.call.count", tool=a, mode="act")
        self.done[(tk, a)] = self.done.get((tk, a), 0) + 1
        if self.store: self.store.record_attempt(tk, a)
        return {"result": result, "approver": p["approver"], "approved_at": time.strftime("%Y-%m-%d %H:%M:%S")}

# ---------------- ITSM: owns ServiceNow ----------------
class ITSM(Agent):
    name, description = "itsm", "Creates, updates, assigns and resolves ServiceNow incidents"
    def __init__(self, snow):
        self.s = snow
        self.skills = {"open_incident": self.open, "add_note": self.note, "assign": self.assign, "resolve": self.resolve}
    def open(self, task):
        i = task.input; existing = self.s.find_open(i["key"])
        if existing: return {"number": existing, "duplicate": True}
        imp, urg = SEVERITY.get(i.get("severity", "major"), ("2", "2"))
        n = self.s.create({"short_description": i["short_description"], "description": i["description"], "impact": imp, "urgency": urg})
        return {"number": n, "duplicate": False}
    def note(self, task): self.s.add_note(task.input["ticket"], task.input["text"]); return {}
    def assign(self, task):
        t, g = task.input["ticket"], task.input["group"]; gid = self.s.ensure_group(g)
        fields = {"state": "2"}
        if gid: fields["assignment_group"] = gid
        self.s.update(t, fields)
        if not gid: self.s.add_note(t, f"Group '{g}' not found in ServiceNow and could not be created; ticket left unassigned. Intended team: {g}.")
        return {"assigned": bool(gid), "group": g}
    def resolve(self, task):
        i = task.input
        self.s.update(i["ticket"], {"state": "6", "close_code": self.s.close_code(), "close_notes": i["notes"]}); return {}

# ---------------- Comms: owns chat ----------------
class Comms(Agent):
    name, description = "comms", "Asks humans for approval and notifies teams (Slack in production)"
    def __init__(self, chat, approvers, approval_channel="#sre-approvals", l2_channel="#sre-l2", ttl=600):
        self.c, self.approvers, self.ch_a, self.ch_l2, self.ttl = chat, set(approvers), approval_channel, l2_channel, ttl
        self.skills = {"request_approval": self.request_approval, "notify_escalation": self.notify}
    def request_approval(self, task):
        i = task.input; rid = f"{i['ticket']}:{i['action']}"
        task.state = "input-required"
        self.c.post_approval(self.ch_a, f":warning: *Approval needed* for {i['ticket']}\nAction: `{i['action']}` on `{i['target']}`\n{i['summary']}\nReply Approve / Reject.", rid)
        deadline = time.time() + self.ttl
        with telemetry.span("approval.wait", action=i["action"], ttl_s=self.ttl) as sp:
            try:
                return self._await(i, rid, deadline, sp)
            except PermissionError as e:
                msg = str(e)
                telemetry.set_attr(sp, "decision", "rejected" if msg.startswith("rejected") else "timeout")
                telemetry.counter("approval.count", decision=sp["attrs"]["decision"])
                raise
    def _await(self, i, rid, deadline, sp):
        try:
            while time.time() < deadline:
                d = self.c.wait_decision(rid, timeout=max(deadline - time.time(), 0.1))
                if d is None: break
                user, decision = d
                if user not in self.approvers:
                    self.c.post(self.ch_a, f"Ignored a response from <@{user}>: not an authorised approver."); continue
                if decision != "approve": raise PermissionError(f"rejected by {user}")
                telemetry.set_attr(sp, "decision", "approved"); telemetry.counter("approval.count", decision="approved")
                return {"decision": "approved", "approver": user, "token": approvals.sign(i["action"], i["target"], i["ticket"], user)}
            raise PermissionError("approval timed out with no authorised response")
        finally:
            getattr(self.c, "close", lambda r: None)(rid)
    def notify(self, task):
        i = task.input
        self.c.post(self.ch_l2, f":rotating_light: *Escalation* {i['ticket']} -> {i['team']}\nReason: {i['reason']}\n{i['summary']}")
        return {"notified": i["team"]}

# ---------------- Triage: owns the incident, delegates by skill ----------------
def decide(rb_id, ev):
    by = {e["tool"]: e["result"] for e in ev}
    if rb_id == "AWS-DISK-001":
        d = by.get("check_disk", {}); ok = d.get("tmp_mb", 0) * 2 > d.get("used_mb", 1)
        return ok, "most space is in F:\\fury-tmp" if ok else "space is NOT mainly in F:\\fury-tmp; clearing temp would not help"
    if rb_id == "AWS-SVC-001":
        txt = " ".join(by.get("read_events", {}).get("events", [])).lower()
        bad = any(k in txt for k in ("out of space", "no space", "at or near capacity"))
        return (not bad), "disk-full events found; fix disk first" if bad else "no disk-full events; restart allowed"
    if rb_id == "AWS-CPU-001":
        c = by.get("check_cpu", {}); ok = c.get("top_process") == "fury-rogue"
        return ok, f"culprit is {c.get('top_process')}" + ("" if ok else " (not allowlisted)")
    return False, "no decision rule for this runbook"

class Triage(Agent):
    name, description = "triage", "Owns the incident from alert to resolution or escalation"
    def __init__(self, bus): self.ticket, self.warnings = None, []; self.bus, self.skills = bus, {"handle_alert": self.handle_alert, "handle_report": self.handle_report}
    def handle_report(self, task):
        """An engineer describes a problem in chat. Same investigation as an alarm, different front door."""
        text = task.input["text"].strip()[:300]; words = set(RB.tokens(text)) | {w for w in text.lower().replace("?", " ").split()}
        metric = next((m for m, kw in REPORT_KEYWORDS if words & kw), None)
        return self.handle_alert(Task(skill="handle_alert", sender="chat", input={
            "name": f"Report-{metric or 'general'}", "description": text, "metric": metric,
            "instance": task.input.get("instance", "i-00654e3403673c840"), "severity": "major"}))
    def ok(self, skill, data):
        t = self.bus.send(self.name, skill, data)
        if t.state != "completed": raise AgentError(f"{skill}: {t.error}")
        return t.output
    def handle_alert(self, task):
        a = task.input; query = f"{a['name']} {a['description']}"
        self.warnings, self.ticket = [], None
        plan = self.ok("match_runbook", {"query": query}); rb = plan["runbook"]
        steps = rb["steps"] if rb else []
        pre = []
        for s in steps:
            if s["type"] != "check": break
            pre.append(s)
        initial = [(s["tool"], s["params"]) for s in pre] if rb else GENERIC_CHECKS.get(a.get("metric"), [])
        ev = self.ok("run_checks", {"steps": [{"tool": t, "params": p} for t, p in initial]})["evidence"] if initial else []
        if ev and ev[0]["result"].get("healthy", False):
            telemetry.counter("triage.outcome", outcome="false_alarm")
            return {"outcome": "false_alarm", "ticket": None, "runbook": rb["id"] if rb else None, "reason": "first check is healthy; alarm not confirmed"}
        key = f"{a['name']} {a['instance']}"
        try:
            tk = self.ok("open_incident", {"key": key, "short_description": f"[{key}] {a['description']}", "severity": a.get("severity", "major"),
                         "description": f"Alarm {a['name']} on {a['instance']}: {a['description']}\nFURY confirmed the issue with read-only checks."})
        except AgentError as e:
            return self.escalate(a, None, rb, f"ticket could not be created ({e}); no action taken without a ticket", ev)
        self.ticket = tk["number"]
        if tk["duplicate"]:
            self.note("Alarm fired again while this incident is open; no new ticket created.")
            telemetry.counter("triage.outcome", outcome="duplicate")
            return {"outcome": "duplicate", "ticket": self.ticket, "runbook": rb["id"] if rb else None, "reason": "open incident already exists"}
        self.note(f"FURY detected alarm {a['name']} ({a['description']}). Runbook: {rb['id'] + ' ' + rb['title'] if rb else 'NONE FOUND'}.\nEvidence:\n" + "\n".join(ev_line(e) for e in ev))
        if not rb: return self.escalate(a, self.ticket, rb, "no runbook covers this issue", ev)
        before, acted, approval = ev, False, None
        for s in steps[len(pre):]:
            if s["type"] == "decide":
                with telemetry.span("agent.decision", runbook=rb["id"], step=s["n"]) as dsp:
                    ok, why = decide(rb["id"], before)
                    telemetry.set_attr(dsp, "proceed", bool(ok)); telemetry.set_attr(dsp, "reason", why)
                self.note(f"Decision ({s['n']}): {why}.")
                if not ok: return self.escalate(a, self.ticket, rb, why, before)
            elif s["type"] == "action":
                self.note(f"Requesting SRE Admin approval for {s['tool']}({s['params'].get('target')}).")
                r = self.bus.send(self.name, "remediate", {"action": s["tool"], "target": s["params"].get("target"), "ticket": self.ticket,
                    "summary": f"{a['description']} on {a['instance']}. Evidence: " + "; ".join(ev_line(e) for e in before)[:400]})
                if r.state != "completed":
                    self.note(f"Action NOT executed: {r.error}"); return self.escalate(a, self.ticket, rb, f"remediation not completed: {r.error}", before)
                acted, approval = True, r.output
                self.note(f"Approved by {approval['approver']} at {approval['approved_at']}. Executed {s['tool']}({s['params'].get('target')}): {short(approval['result'])}")
            elif s["type"] == "check" and acted:
                after = self.ok("run_checks", {"steps": [{"tool": s["tool"], "params": s["params"]}]})["evidence"]
                self.note("Verification:\n" + "\n".join(ev_line(e) for e in after))
                if after[0]["result"].get("healthy"):
                    notes = (f"Issue: {a['description']} on {a['instance']}.\nRunbook: {rb['id']}.\nBefore: " + "; ".join(ev_line(e) for e in before[:1]) +
                             f"\nAction: {s_action(steps)} approved by {approval['approver']} at {approval['approved_at']}.\nAfter: {ev_line(after[0])}\nResolved by FURY; service verified healthy.")
                    self.ok("resolve", {"ticket": self.ticket, "notes": notes})
                    telemetry.counter("triage.outcome", outcome="resolved", runbook=rb["id"])
                    return {"outcome": "resolved", "ticket": self.ticket, "runbook": rb["id"], "reason": "verified healthy after approved fix"}
                return self.escalate(a, self.ticket, rb, "verification failed after the fix", before + after)
            elif s["type"] == "escalate": break
        return self.escalate(a, self.ticket, rb, "runbook ended without a verified fix", before)
    def note(self, text):
        try: self.ok("add_note", {"ticket": self.ticket, "text": text})
        except AgentError as e: self.warnings.append(str(e))
    def escalate(self, a, ticket, rb, reason, ev):
        team = rb["escalate_to"] if rb else "SRE-Platform L2"
        summary = "Evidence: " + "; ".join(ev_line(e) for e in ev)[:600]
        if ticket:
            try:
                r = self.ok("assign", {"ticket": ticket, "group": team}); self.note(f"ESCALATED to {team}: {reason}.\n{summary}")
            except AgentError as e: self.warnings.append(str(e))
        try: self.ok("notify_escalation", {"ticket": ticket or "(no ticket)", "team": team, "reason": reason, "summary": summary})
        except AgentError as e: self.warnings.append(str(e))
        telemetry.counter("triage.outcome", outcome="escalated", team=team)
        return {"outcome": "escalated", "ticket": ticket, "runbook": rb["id"] if rb else None, "reason": reason, "team": team}

def s_action(steps): return next((f"{s['tool']}({s['params'].get('target')})" for s in steps if s["type"] == "action"), "")
