"""Finds the Resolution code value this ServiceNow instance accepts. Creates ONE throw-away incident. Prints no secrets.   python snow_debug.py"""
import os, time
from run_fury import load_env
load_env()
from fury.adapters import SnowClient, SnowError

c = SnowClient(os.environ["SNOW_URL"], os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
def tryget(label, path, params):
    try: return c._req("GET", path, params=params)
    except SnowError as e: print(f"  {label}: not readable ({str(e).split('HTTP')[-1][:60].strip()})"); return None

print("1) Asking the instance for the list of resolution codes...")
found = []
r = tryget("sys_choice", "/api/now/table/sys_choice", {"sysparm_query": "element=close_code^nameINincident,task", "sysparm_fields": "value,label,inactive,name"})
if r:
    print(f"  sys_choice returned {len(r)} rows:", [(x["value"], x.get("label"), x["inactive"]) for x in r][:15]); found += [x["value"] for x in r if x.get("inactive") != "true"]
elif r == []: print("  sys_choice returned 0 rows (probably not readable by this user)")
m = tryget("ui meta", "/api/now/ui/meta/incident", {})
try:
    ch = m["columns"]["close_code"]["choices"]; print("  ui meta choices:", [(x.get("value"), x.get("label")) for x in ch]); found += [x["value"] for x in ch if x.get("value")]
except Exception: print("  ui meta did not list close_code choices")

CLASSIC = ["Solved (Work Around)", "Solved (Permanently)", "Solved Remotely (Work Around)", "Solved Remotely (Permanently)", "Not Solved (Not Reproducible)", "Not Solved (Too Costly)",
           "Closed/Resolved by Caller", "Resolved by caller", "Solution provided", "Fix applied", "Resolved", os.getenv("SNOW_CLOSE_CODE", "")]
cands = list(dict.fromkeys([x for x in found + CLASSIC if x]))
print(f"\n2) Testing {len(cands)} candidate values on one throw-away incident (a value is 'accepted' only if ServiceNow actually stores it)...")
n = c.create({"short_description": f"[FURY snow_debug] {int(time.time())}", "description": "diagnostic, safe to ignore", "impact": "3", "urgency": "3"})
sid = c._sys_id(n); accepted = []
def stored(): return c._req("GET", f"/api/now/table/incident/{sid}", params={"sysparm_fields": "close_code"}).get("close_code", "")
for v in cands:
    try: c.update(n, {"close_code": v}); got = stored()
    except SnowError as e: got = "ERR " + str(e).split("HTTP")[-1][:60]
    ok = bool(got) and not got.startswith("ERR")
    print(f"  {'ACCEPTED' if ok else 'rejected':9} {v!r:42} stored={got!r}")
    if ok: accepted.append(got)

print()
if accepted:
    good = accepted[0]
    try:
        c.update(n, {"state": "6", "close_code": good, "close_notes": "snow_debug resolve test"}); print(f"3) Full resolve with {good!r}: OK")
        print(f"\n>>> Put this line in .env, then rerun check_all.py:\nSNOW_CLOSE_CODE={good}")
    except SnowError as e: print(f"3) Code is accepted but resolve still fails: {str(e)[:260]}")
else:
    print("No value was accepted. That means this user cannot WRITE the Resolution code field (field-level permission), or the field was customised.")
    print("Fix (as admin): open the user record, check Roles tab, give it 'itil' and 'itil_admin' if missing; if it still fails, send me this output.")
print(f"\nTest incident: {n} (safe to delete or ignore)")
