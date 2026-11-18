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
