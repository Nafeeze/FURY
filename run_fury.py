"""Start FURY.   python run_fury.py          (real: ServiceNow + AWS + Slack, UI at http://127.0.0.1:8080)
                python run_fury.py --demo   (all fakes, nothing external; has buttons to break the fake server)"""
import os, sys, time, threading
from pathlib import Path
from fury import system
from fury.webapp import Ctx, create_app

def load_env(path=".env"):
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    """Each component is chosen independently: FURY_SNOW / FURY_TARGET / FURY_CHAT = real | fake  (--demo makes all three fake)."""
    load_env(); demo = "--demo" in sys.argv
    mode = {k: ("fake" if demo else os.getenv(f"FURY_{k.upper()}", "real").lower()) for k in ("snow", "target", "chat")}
    approvers = [x.strip() for x in os.getenv("SLACK_APPROVER_IDS", "U0CAZH32V2T").split(",") if x.strip()]
    inst = os.getenv("AWS_INSTANCE_ID", "i-00654e3403673c840"); region = os.getenv("AWS_REGION", "ap-south-2")
    from fury.adapters import FakeTarget, FakeSnow, FakeChat, SnowClient
    kw, poller, fake_target = {}, None, None
    # ---- ServiceNow ----
    snow = SnowClient(os.environ["SNOW_URL"], os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"]) if mode["snow"] == "real" else FakeSnow()
    # ---- chat / approvals ----
    if mode["chat"] == "real":
        from fury.slack_chat import SlackChat
        chat = SlackChat(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_APP_TOKEN"], os.getenv("SLACK_APPROVALS_CHANNEL", "#sre-approvals"), os.getenv("SLACK_L2_CHANNEL", "#sre-l2"))
        chat.start()
    else:
        class DemoChat(FakeChat):
            def __init__(s): super().__init__(); s._p = {}
            def post_approval(s, ch, text, rid): s._p[rid] = {"text": text, "since": time.strftime("%H:%M:%S")}; return super().post_approval(ch, text, rid)
            def wait_decision(s, rid, timeout): time.sleep(5); return (approvers[0], "approve")   # pretend the SRE Admin taps Approve after 5s
            def close(s, rid): s._p.pop(rid, None)
            def pending(s): return [{"request": r, **v} for r, v in s._p.items()]
        chat = DemoChat()
    # ---- server ----
    diag = None
    if mode["target"] == "real":
        from boto3 import Session
        from fury.aws import AwsReadTarget, AwsActTarget
        mk = lambda p: Session(aws_access_key_id=os.environ[p + "_KEY_ID"], aws_secret_access_key=os.environ[p + "_SECRET"], region_name=region)
        diag, remed = mk("AWS_DIAG"), mk("AWS_REMED")
        kw = dict(read_target=AwsReadTarget(diag, region, inst), act_target=AwsActTarget(remed, region, inst))
    else:
        fake_target = FakeTarget(); kw = dict(target=fake_target)
    bus, triage, env = system.build(snow=snow, chat=chat, approvers=approvers, **kw)
    ctx = Ctx(bus, triage, snow, chat, inst, fake_target)
    if diag:
        from fury.aws import AlarmPoller
        poller = AlarmPoller(diag.client("cloudwatch", region_name=region), inst, lambda a: ctx.submit("handle_alert", a, "cloudwatch", a["description"]),
                             interval=int(os.getenv("POLL_SECONDS", "30")))
        ctx.poller = poller; threading.Thread(target=poller.run_forever, daemon=True).start()
    app = create_app(ctx, demo=fake_target is not None)
    print(f"FURY running  snow={mode['snow']}  server={mode['target']}  chat={mode['chat']}  -> http://127.0.0.1:8080")
    app.run(host="127.0.0.1", port=8080, threaded=True)

if __name__ == "__main__": main()
