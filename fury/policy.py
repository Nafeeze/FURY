"""Action risk policy (autonomy tiers). The backend decides how much human control an action needs; no model can.

  read_only   : inspect only (diagnostic tools). Never goes through remediation.
  policy      : a bounded, pre-approved action may run without a human, ONLY if explicitly enabled by config.
  human       : durable approval by an authorised person before execution (default for every catalog action).
  restricted  : denied outright. Anything not in the catalog is restricted (deny by default).

Override per action with FURY_ACTION_TIERS="clear_temp_files=policy,kill_process=human". 'restricted' and 'human'
can always be set; 'policy' only applies to actions that exist in the approved catalog."""
import os

READ_ONLY, POLICY, HUMAN, RESTRICTED = "read_only", "policy", "human", "restricted"
TIERS = (READ_ONLY, POLICY, HUMAN, RESTRICTED)
DEFAULT_TIERS = {"restart_service": HUMAN, "clear_temp_files": HUMAN, "kill_process": HUMAN}

def _overrides():
    out = {}
    for part in os.getenv("FURY_ACTION_TIERS", "").split(","):
        if "=" in part:
            k, v = (x.strip().lower() for x in part.split("=", 1))
            if v in (POLICY, HUMAN, RESTRICTED): out[k] = v
    return out

def tier(action):
    t = _overrides().get(action) or DEFAULT_TIERS.get(action, RESTRICTED)
    if action not in DEFAULT_TIERS: return RESTRICTED     # unknown actions can never be loosened by config
    return t

def requires_human(action): return tier(action) == HUMAN
def allowed(action): return tier(action) != RESTRICTED
