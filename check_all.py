"""Verify every REAL connection in one run. Prints PASS/FAIL per line. Never prints secrets.   python check_all.py"""
import os, sys, time
from run_fury import load_env
load_env()
res = []
ONLY = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None   # snow | aws | slack
def group(name):
    if name.startswith("ServiceNow"): return "snow"
    if name.startswith("Slack"): return "slack"
    if name.startswith((".env", "python")): return "core"
    return "aws"
def step(name, fn):
    if ONLY and group(name) not in (ONLY, "core"): return
    try: detail = fn(); res.append(True); print(f"PASS  {name}" + (f"  ({detail})" if detail else ""))
    except Exception as e: res.append(False); print(f"FAIL  {name}  -> {type(e).__name__}: {str(e)[:220]}")
need = ["SNOW_URL", "SNOW_USER", "SNOW_PASSWORD", "AWS_DIAG_KEY_ID", "AWS_DIAG_SECRET", "AWS_REMED_KEY_ID", "AWS_REMED_SECRET", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "APPROVAL_SECRET"]
def env():
    keys = [k for k in need if not ONLY or k.startswith({"snow": "SNOW_", "aws": "AWS_", "slack": "SLACK_"}[ONLY])]
    miss = [k for k in keys if not os.getenv(k)]
    if miss: raise RuntimeError("missing in .env: " + ", ".join(miss))
    return f"{len(keys)} keys present"
step(".env has every key", env)
REG, INST = os.getenv("AWS_REGION", "ap-south-2"), os.getenv("AWS_INSTANCE_ID", "i-00654e3403673c840")
step("python packages", lambda: [__import__(m) for m in (("flask",) if ONLY == "snow" else ("flask", "boto3", "slack_bolt"))] and "required packages import")

# ---- ServiceNow: create, note, resolve a throw-away incident ----
def snow_flow():
    from fury.adapters import SnowClient
    c = SnowClient(os.environ["SNOW_URL"], os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
    n = c.create({"short_description": f"[FURY check_all] connectivity test {int(time.time())}", "description": "Created by check_all.py; safe to ignore.", "impact": "3", "urgency": "3"})
    c.add_note(n, "FURY check_all: work note write OK")
    c.update(n, {"state": "6", "close_code": c.close_code(), "close_notes": "check_all test incident, resolved automatically"})
    notes = c.get_notes(n)
    return f"created, noted and resolved {n} with resolution code '{c.close_code()}'; read back {len(notes)} work note(s)"
def codes():
    from fury.adapters import SnowClient
    c = SnowClient(os.environ["SNOW_URL"], os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"]); code = c.close_code()
    if not c.close_choices and not os.getenv("SNOW_CLOSE_CODE"):
        raise RuntimeError("could not read the resolution code list; open any incident in ServiceNow, copy a value from the Resolution code dropdown and put SNOW_CLOSE_CODE=<value> in .env")
    return f"using '{code}' from {len(c.close_choices)} choices: {c.close_choices}"
step("ServiceNow: resolution code choices", codes)
step("ServiceNow: create + work note + resolve", snow_flow)
def groups():
    from fury.adapters import SnowClient
    c = SnowClient(os.environ["SNOW_URL"], os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
    if c.find_group("SRE-Platform L2"): return "group 'SRE-Platform L2' exists"
    raise RuntimeError("group 'SRE-Platform L2' not found: in ServiceNow go to User Administration > Groups > New, name it exactly SRE-Platform L2")
step("ServiceNow: L2 assignment group", groups)

# ---- AWS ----
try:
    from boto3 import Session
except ImportError:
    Session = None
def sess(p): return Session(aws_access_key_id=os.environ[p + "_KEY_ID"], aws_secret_access_key=os.environ[p + "_SECRET"], region_name=REG)
def who(p): return lambda: sess(p).client("sts").get_caller_identity()["Arn"].split("/")[-1]
step("AWS identity: diagnostic user", who("AWS_DIAG")); step("AWS identity: remediation user", who("AWS_REMED"))
def online():
    r = sess("AWS_DIAG").client("ssm").describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [INST]}])["InstanceInformationList"]
    if not r or r[0]["PingStatus"] != "Online": raise RuntimeError("instance not Online in Systems Manager (Fleet Manager)")
    return r[0].get("PlatformName", "") + " " + r[0].get("PlatformVersion", "")
step("EC2 is Online in Systems Manager", online)
def real_check():
    from fury.aws import AwsReadTarget
    t = AwsReadTarget(sess("AWS_DIAG"), REG, INST)
    s, d = t.check_service("W3SVC"), t.check_disk("F:")
    return f"W3SVC={s.get('state')}, F: used {d.get('used_percent')}%"
step("Real read-only checks over SSM (service + disk)", real_check)
def priv():
    from fury.aws import _Ssm
    try: _Ssm(sess("AWS_DIAG").client("ssm"), INST).run("Fury-Act-RestartService", {"Name": ["W3SVC"]})
    except Exception as e:
        if "AccessDenied" in str(e) or "not authorized" in str(e).lower(): return "diagnostic user was correctly DENIED from running an action"
        raise
    raise RuntimeError("diagnostic user was able to run an ACTION: least privilege is broken")
step("Least privilege: read-only user cannot act", priv)
def alarms():
    a = sess("AWS_DIAG").client("cloudwatch").describe_alarms(AlarmNamePrefix="Fury-")["MetricAlarms"]
    if len(a) < 4: raise RuntimeError(f"expected 4 Fury- alarms, found {len(a)}; run setup_aws.py")
    return ", ".join(f"{x['AlarmName']}={x['StateValue']}" for x in a)
step("CloudWatch alarms exist", alarms)
def metrics():
    m = sess("AWS_DIAG").client("cloudwatch").list_metrics(Namespace="Fury")["Metrics"]
    if not m: raise RuntimeError("no Fury metrics yet: wait 2-3 minutes after setup, or the metrics publisher on the EC2 is failing")
    return f"{len({x['MetricName'] for x in m})} metrics flowing"
step("EC2 is publishing metrics", metrics)

# ---- Slack ----
def slack():
    from slack_sdk import WebClient
    c = WebClient(token=os.environ["SLACK_BOT_TOKEN"]); a = c.auth_test()
    ids = [x.strip() for x in os.getenv("SLACK_APPROVER_IDS", "U0CAZH32V2T").split(",")]
    for u in ids: c.users_info(user=u)
    ch = os.getenv("SLACK_APPROVALS_CHANNEL", "#sre-approvals").lstrip("#")
    cid = next((x["id"] for x in c.conversations_list(types="public_channel,private_channel", limit=500)["channels"] if x["name"] == ch), None)
    if not cid: raise RuntimeError(f"bot cannot see #{ch}: create it and run /invite @{a['user']}")
    c.chat_postMessage(channel=cid, text="FURY check_all: Slack connection OK")
    return f"bot '{a['user']}' posted to #{ch}; approver id(s) valid"
step("Slack: bot token, approver, channel, post", slack)
def sock():
    from slack_sdk.web import WebClient
    r = WebClient(token=os.environ["SLACK_APP_TOKEN"]).api_call("apps.connections.open")
    if not r["ok"]: raise RuntimeError("app-level token rejected")
    return "Socket Mode token OK"
step("Slack: Socket Mode app token", sock)
print(f"\n{sum(res)}/{len(res)} checks passed.  " + ("Ready: run  python run_fury.py" if all(res) else "Fix the FAIL lines, paste them to Claude (no secrets in them), rerun."))
sys.exit(0 if all(res) else 1)
