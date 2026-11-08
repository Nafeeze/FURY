"""Week 4 style BASELINE: one agent that holds every tool (server reads, server actions, ServiceNow, chat) and calls them directly.
Same runbooks, same rules, same approvers. The only difference from the multi-agent version is the architecture."""
import time
from . import runbooks as RB, approvals
from .agents import GENERIC_CHECKS, ACTION_CATALOG, decide, ev_line, short, SEVERITY

class SingleAgent:
    def __init__(self, runbook_list, target, snow, chat, approvers=("U0CAZH32V2T",), ttl=600):
        self.rbs, self.t, self.s, self.c, self.approvers, self.ttl, self.calls, self.did = runbook_list, target, snow, chat, set(approvers), ttl, 0, {}
    def tool(self, _fn, **kw): self.calls += 1; return getattr(self.t, _fn)(**kw)
    def note(self, n, text):
        self.calls += 1
        try: self.s.add_note(n, text)
        except Exception: pass
    def tools_held(self): return 5 + 3 + 6 + 2    # 5 server reads, 3 server actions, 6 ServiceNow ops, 2 chat ops
    def handle(self, a):
        rb, _ = RB.match(self.rbs, f"{a['name']} {a['description']}"); steps = rb.as_dict()["steps"] if rb else []
        pre = []
        for s in steps:
            if s["type"] != "check": break
            pre.append(s)
        init = [(s["tool"], s["params"]) for s in pre] if rb else GENERIC_CHECKS.get(a.get("metric"), [])
        ev = [{"tool": t, "params": p, "result": self.tool(t, **p)} for t, p in init]
        if ev and ev[0]["result"].get("healthy"): return {"outcome": "false_alarm", "ticket": None}
        key = f"{a['name']} {a['instance']}"; self.calls += 1
        try:
            n = self.s.find_open(key)
            if n: self.note(n, "Alarm fired again."); return {"outcome": "duplicate", "ticket": n}
            imp, urg = SEVERITY.get(a.get("severity", "major"), ("2", "2"))
            n = self.s.create({"short_description": f"[{key}] {a['description']}", "description": a["description"], "impact": imp, "urgency": urg})
        except Exception:
            return self.escalate(a, None, rb, "ticket could not be created", ev)
        self.note(n, f"FURY detected alarm {a['name']}. Runbook: {rb.id if rb else 'NONE FOUND'}.\nEvidence:\n" + "\n".join(ev_line(e) for e in ev))
        if not rb: return self.escalate(a, n, rb, "no runbook covers this issue", ev)
        acted = None
        for s in steps[len(pre):]:
            if s["type"] == "decide":
                ok, why = decide(rb.id, ev); self.note(n, f"Decision ({s['n']}): {why}.")
                if not ok: return self.escalate(a, n, rb, why, ev)
            elif s["type"] == "action":
                act, tgt = s["tool"], s["params"].get("target")
                if act not in ACTION_CATALOG or tgt not in ACTION_CATALOG[act] or self.did.get((n, act)): return self.escalate(a, n, rb, "action not allowed", ev)
                self.note(n, f"Requesting SRE Admin approval for {act}({tgt}).")
                self.calls += 1; self.c.post_approval("#sre-approvals", f"Approval needed for {n}: {act} on {tgt}", f"{n}:{act}")
                who = None
                while True:
                    self.calls += 1; d = self.c.wait_decision(f"{n}:{act}", self.ttl)
                    if d is None: break
                    if d[0] in self.approvers: who = d; break
                if not who or who[1] != "approve": self.note(n, "Action NOT executed: no authorised approval."); return self.escalate(a, n, rb, "remediation not completed: approval not granted", ev)
                res = self.tool("act", action=act, target=tgt); self.did[(n, act)] = 1
                acted = (who[0], time.strftime("%Y-%m-%d %H:%M:%S"), act, tgt)
                self.note(n, f"Approved by {who[0]} at {acted[1]}. Executed {act}({tgt}): {short(res)}")
            elif s["type"] == "check" and acted:
                after = [{"tool": s["tool"], "params": s["params"], "result": self.tool(s["tool"], **s["params"])}]
                self.note(n, "Verification:\n" + "\n".join(ev_line(e) for e in after))
                if after[0]["result"].get("healthy"):
                    self.calls += 1
                    self.s.update(n, {"state": "6", "close_code": self.s.close_code(), "close_notes":
                        f"Issue: {a['description']}.\nRunbook: {rb.id}.\nBefore: {ev_line(ev[0])}\nAction: {acted[2]}({acted[3]}) approved by {acted[0]} at {acted[1]}.\nAfter: {ev_line(after[0])}\nResolved by FURY; service verified healthy."})
                    return {"outcome": "resolved", "ticket": n}
                return self.escalate(a, n, rb, "verification failed after the fix", ev + after)
            elif s["type"] == "escalate": break
        return self.escalate(a, n, rb, "runbook ended without a verified fix", ev)
    def escalate(self, a, n, rb, reason, ev):
        team = rb.escalate_to if rb else "SRE-Platform L2"
        if n:
            self.calls += 1
            try:
                gid = self.s.ensure_group(team); self.s.update(n, {"state": "2", **({"assignment_group": gid} if gid else {})})
            except Exception: pass
            self.note(n, f"ESCALATED to {team}: {reason}.\nEvidence: " + "; ".join(ev_line(e) for e in ev)[:600])
        self.calls += 1; self.c.post("#sre-l2", f"Escalation {n or '(no ticket)'} -> {team}: {reason}")
        return {"outcome": "escalated", "ticket": n}
