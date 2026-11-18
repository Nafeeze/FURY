"""Service-level objectives computed from live telemetry. Each SLO says what 'good' means and whether we are meeting it
right now. Status: ok | breach | no_data (never reports 'ok' without evidence). Thresholds are env-tunable."""
import os
from . import telemetry


def _f(name, default):
    return float(os.getenv(name, default))


def _counts(name, label):
    return telemetry._by(name, label)


def evaluate():
    spans = telemetry._fallback.snapshot_spans()
    oc = _counts("triage.outcome", "outcome"); total = sum(oc.values())
    calls = _counts("integration.call", "outcome"); calls_total = sum(calls.values())
    hops = [x["ms"] for x in spans if x["name"] == "agent.hop" and x["attrs"].get("skill") not in ("request_approval", "remediate", "handle_alert", "handle_report")]
    viol = sum(_counts("contract.violation", "skill").values())
    out = []

    def slo(id_, desc, objective, measured, ok, unit=""):
        status = "no_data" if measured is None else ("ok" if ok else "breach")
        out.append({"id": id_, "description": desc, "objective": objective, "measured": measured, "unit": unit, "status": status})

    stopped = sum(_counts("workflow.budget_stopped", "x").values())
    comp = None if not total else round(1 - stopped / total, 3)
    slo("workflow_completion", "Workflows finish inside their budget (not stopped by the budget guard)", f">= {_f('FURY_SLO_COMPLETION', '0.95')}", comp,
        comp is not None and comp >= _f("FURY_SLO_COMPLETION", "0.95"))
    esc = None if not total else round(oc.get("escalated", 0) / total, 3)
    slo("escalation_rate", "Share of incidents handed to humans (high = runbooks not covering real incidents)", f"<= {_f('FURY_SLO_ESCALATION', '0.5')}", esc,
        esc is not None and esc <= _f("FURY_SLO_ESCALATION", "0.5"))
    p95 = telemetry._p95(hops)
    slo("agent_hop_p95", "Agent hop latency p95 (excludes human-wait hops)", f"<= {_f('FURY_SLO_HOP_P95_MS', '5000')} ms", p95,
        p95 is not None and p95 <= _f("FURY_SLO_HOP_P95_MS", "5000"), "ms")
    err = None if not calls_total else round((calls.get("error", 0) + calls.get("circuit_open", 0)) / calls_total, 3)
    slo("integration_errors", "Share of integration calls that failed", f"<= {_f('FURY_SLO_INTEGRATION_ERR', '0.05')}", err,
        err is not None and err <= _f("FURY_SLO_INTEGRATION_ERR", "0.05"))
    slo("contract_violations", "Malformed agent requests rejected (should be zero in normal operation)", "== 0", viol if (total or viol) else None, viol == 0)
    breaches = [s["id"] for s in out if s["status"] == "breach"]
    return {"status": "breach" if breaches else ("no_data" if all(s["status"] == "no_data" for s in out) else "ok"), "breaches": breaches, "slos": out}
