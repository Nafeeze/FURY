"""Versioned input contracts for every agent skill. The Bus validates a request against its contract BEFORE any
agent runs, so malformed or hostile input (from a model, a ticket, a remote agent) fails fast with a clear error
instead of reaching a tool. Contracts are deliberately small: required keys and types, nothing clever.
Add fields, never repurpose them; bump CONTRACT_VERSION on breaking changes."""
CONTRACT_VERSION = "1.0"
S, L = str, list
CONTRACTS = {
    "handle_alert":      {"name": S, "description": S, "instance": S},
    "handle_report":     {"text": S},
    "match_runbook":     {"query": S},
    "run_checks":        {"steps": L},
    "open_incident":     {"key": S, "short_description": S, "description": S},
    "add_note":          {"ticket": S, "text": S},
    "assign":            {"ticket": S, "group": S},
    "resolve":           {"ticket": S, "notes": S},
    "request_approval":  {"action": S, "target": S, "ticket": S, "summary": S},
    "remediate":         {"action": S, "target": S, "ticket": S, "summary": S},
    "notify_escalation": {"ticket": S, "team": S, "reason": S, "summary": S},
}

# What each skill must return. Checked by the Bus on every completed task, so a buggy or remote agent that returns the
# wrong shape fails that hop visibly instead of corrupting a later step. Same rule as inputs: required keys and types.
N, NUM, ANY = type(None), (int, float), object
OUTPUTS = {
    "handle_alert":      {"outcome": S, "reason": S, "ticket": (S, N)},
    "handle_report":     {"outcome": S, "reason": S, "ticket": (S, N)},
    "match_runbook":     {"runbook": (dict, N), "score": NUM},
    "run_checks":        {"evidence": L},
    "open_incident":     {"number": S, "duplicate": bool},
    "add_note":          {},
    "assign":            {"assigned": bool, "group": S},
    "resolve":           {},
    "request_approval":  {"decision": S, "approver": S, "token": S},
    "remediate":         {"result": ANY, "approver": S, "approved_at": S},
    "notify_escalation": {"notified": S},
}
EVIDENCE_KEYS = {"tool": str, "params": dict, "result": ANY, "evidence_id": str, "observed_at": str}

def _name(typ): return "/".join(t.__name__ for t in (typ if isinstance(typ, tuple) else (typ,)))

def validate_output(skill, out):
    """Return None if a skill's output has the agreed shape, else a short reason. Unknown skills are not this module's concern."""
    spec = OUTPUTS.get(skill)
    if spec is None: return None
    if not isinstance(out, dict): return "output must be an object"
    for key, typ in spec.items():
        if key not in out: return f"missing required field '{key}'"
        v = out[key]
        if typ is not ANY and not isinstance(v, typ): return f"field '{key}' must be {_name(typ)}"
        if typ is NUM and isinstance(v, bool): return f"field '{key}' must be {_name(typ)}"   # bool is an int in Python; a score is not a flag
    if skill == "run_checks":
        for i, e in enumerate(out["evidence"]):
            if not isinstance(e, dict): return f"evidence[{i}] must be an object"
            for k, typ in EVIDENCE_KEYS.items():
                if k not in e: return f"evidence[{i}] is missing '{k}'"
                if typ is not ANY and not isinstance(e[k], typ): return f"evidence[{i}].{k} must be {_name(typ)}"
    return None

def validate(skill, data):
    """Return None if valid, else a short reason. Unknown skills are not this module's concern."""
    spec = CONTRACTS.get(skill)
    if spec is None: return None
    if not isinstance(data, dict): return "input must be an object"
    for key, typ in spec.items():
        if key not in data: return f"missing required field '{key}'"
        if not isinstance(data[key], typ): return f"field '{key}' must be {typ.__name__}"
    if skill == "run_checks":
        for i, st in enumerate(data["steps"]):
            if not isinstance(st, dict) or not isinstance(st.get("tool"), str): return f"steps[{i}] needs a string 'tool'"
            if "params" in st and not isinstance(st["params"], dict): return f"steps[{i}].params must be an object"
    return None
