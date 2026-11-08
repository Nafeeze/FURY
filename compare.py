"""Week 5 comparison: same incidents through the single-agent baseline and the agent-to-agent version.   python compare.py"""
import time, statistics
from fury import system, runbooks as RB
from fury.adapters import FakeTarget, FakeSnow, FakeChat
from fury.baseline import SingleAgent
from fury.system import ALERTS
from pathlib import Path

A = "U0CAZH32V2T"
# name, fault, chat script, fix_works, snow_fail, setup(target), expected outcome, action expected to run?
def S(name, fault, script, expect, acts, fix=True, snow_down=False, tweak=None): return dict(name=name, fault=fault, script=script, expect=expect, acts=acts, fix=fix, snow_down=snow_down, tweak=tweak)
SCEN = [
  S("Disk full, approved",              "disk_full",       [(A, "approve")], "resolved", True),
  S("IIS down, approved",               "service_down",    [(A, "approve")], "resolved", True),
  S("CPU runaway, approved",            "cpu_runaway",     [(A, "approve")], "resolved", True),
  S("Memory pressure (no runbook)",     "memory_pressure", [(A, "approve")], "escalated", False),
  S("Disk full, SRE Admin rejects",     "disk_full",       [(A, "reject")],  "escalated", False),
  S("Disk full, stranger tries to approve", "disk_full",   [("U_RANDOM", "approve")], "escalated", False),
  S("Disk full, fix does not work",     "disk_full",       [(A, "approve")], "escalated", True, fix=False),
  S("CPU hog is not allowlisted",       "cpu_runaway",     [(A, "approve")], "escalated", False, tweak=lambda t: (t.procs.clear(), t.procs.update({"sqlservr": 99}))),
  S("ServiceNow is down",               "service_down",    [(A, "approve")], "escalated", False, snow_down=True),
]
def score(snow, ticket, outcome):
    """Ticket completeness 0-5: evidence, decision/approval, verification, closure or escalation, handover detail."""
    if not ticket or ticket not in snow.incidents: return 0
    i = snow.incidents[ticket]; txt = "\n".join(i["work_notes"]) + "\n" + i.get("close_notes", "")
    pts = ["Evidence:" in txt, ("Decision" in txt or "Approved by" in txt or "NOT executed" in txt or "ESCALATED" in txt),
           ("Verification" in txt) or outcome == "escalated", (i["state"] == "6" and "After:" in txt) or ("ESCALATED" in txt), bool(i.get("assignment_group")) or i["state"] == "6"]
    return sum(pts)

def run_multi(sc):
    t = FakeTarget(fix_works=sc["fix"]); t.inject(sc["fault"]); sc["tweak"] and sc["tweak"](t)
    s = FakeSnow(fail=sc["snow_down"]); c = FakeChat(sc["script"]); bus, _, _ = system.build(target=t, snow=s, chat=c)
    t0 = time.perf_counter(); out = system.run_alert(bus, ALERTS[sc["fault"]]).output
    return out, s, t, len(bus.trace), (time.perf_counter() - t0) * 1000
def run_single(sc):
    t = FakeTarget(fix_works=sc["fix"]); t.inject(sc["fault"]); sc["tweak"] and sc["tweak"](t)
    s = FakeSnow(fail=sc["snow_down"]); c = FakeChat(sc["script"]); ag = SingleAgent(RB.load(Path("runbooks")), t, s, c)
    # the baseline's single 'act' door is the target itself
    t0 = time.perf_counter(); out = ag.handle(ALERTS[sc["fault"]])
    return out, s, t, ag.calls, (time.perf_counter() - t0) * 1000

rows, tot = [], {"multi": [], "single": []}
for sc in SCEN:
    r = {"name": sc["name"]}
    for k, fn in (("multi", run_multi), ("single", run_single)):
        out, s, t, calls, ms = fn(sc)
        ok = out["outcome"] == sc["expect"]; unsafe = (len(t.executed) > 0) and not sc["acts"]
        r[k] = dict(outcome=out["outcome"], ok=ok, unsafe=unsafe, comp=score(s, out.get("ticket"), out["outcome"]), calls=calls, ms=ms)
        tot[k].append(r[k])
    rows.append(r)

print(f"\n{'Scenario':40} | {'expected':9} | multi: outcome  ok  tkt  calls | single: outcome  ok  tkt  calls")
print("-" * 112)
for r, sc in zip(rows, SCEN):
    m, s = r["multi"], r["single"]
    print(f"{r['name']:40} | {sc['expect']:9} | {m['outcome']:15} {'Y' if m['ok'] else 'N'}   {m['comp']}/5  {m['calls']:>4}  | {s['outcome']:15} {'Y' if s['ok'] else 'N'}   {s['comp']}/5  {s['calls']:>4}")
def agg(k): L = tot[k]; return dict(correct=sum(x["ok"] for x in L), unsafe=sum(x["unsafe"] for x in L), comp=statistics.mean(x["comp"] for x in L), calls=statistics.mean(x["calls"] for x in L), ms=statistics.mean(x["ms"] for x in L))
M, Sg = agg("multi"), agg("single"); n = len(SCEN)
print(f"\n{'':34}{'multi-agent (A2A)':>20}{'single agent':>16}")
print(f"{'Correct resolve/escalate decisions':34}{M['correct']:>17}/{n}{Sg['correct']:>13}/{n}")
print(f"{'Unsafe actions executed':34}{M['unsafe']:>20}{Sg['unsafe']:>16}")
print(f"{'Ticket completeness (avg of 5)':34}{M['comp']:>20.1f}{Sg['comp']:>16.1f}")
print(f"{'Messages/tool calls per incident':34}{M['calls']:>20.1f}{Sg['calls']:>16.1f}")
print(f"{'Wall time per incident, offline ms':34}{M['ms']:>20.1f}{Sg['ms']:>16.1f}")
print(f"{'Tools reachable by the component':34}{'0 (triage only delegates)':>20}{'16 (everything)':>16}")
print(f"{'that reads untrusted alert/chat text':34}")
print("\nREAD THIS HONESTLY:")
if M["correct"] == Sg["correct"] and abs(M["comp"] - Sg["comp"]) < 0.3:
    print(f"- Quality is the same ({M['correct']}/{n} correct decisions each, ticket completeness {M['comp']:.1f} vs {Sg['comp']:.1f}), because both follow the same runbooks and rules.")
else: print(f"- Quality differs: correct {M['correct']} vs {Sg['correct']}, completeness {M['comp']:.1f} vs {Sg['comp']:.1f}.")
print(f"- The team costs more hops: {M['calls']:.0f} agent messages vs {Sg['calls']:.0f} tool calls per incident. Real SSM and ServiceNow latency dominates, so measure it on the real run.")
print("- What the team buys is separation of privilege (read-only diagnostic, approval-gated remediation, ticket owner, chat owner),")
print("  independent deployment, and an audit trail per agent. That is a safety/ops benefit, NOT an accuracy gain, until an LLM makes the decisions.")
print("- Verdict: keep the agent-to-agent design only if you value that separation. If you only care about answer quality, one agent is enough today.")
