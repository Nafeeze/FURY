"""Real AWS adapters (boto3 imported lazily). Two SEPARATE credential sets:
   read session  -> AwsReadTarget  (can only run Fury-Check-* SSM documents, read CloudWatch)
   act session   -> AwsActTarget   (can only run Fury-Act-* SSM documents)
Parameter values are validated again inside the SSM documents themselves (allowedValues / allowedPattern)."""
import json, time, threading

READ_DOCS = {"check_disk": "Fury-Check-Disk", "check_cpu": "Fury-Check-Cpu", "check_service": "Fury-Check-Service",
             "check_memory": "Fury-Check-Memory", "read_events": "Fury-Check-Events"}
ACT_DOCS = {"restart_service": ("Fury-Act-RestartService", "Name"), "clear_temp_files": ("Fury-Act-ClearTemp", "Path"),
            "kill_process": ("Fury-Act-KillProcess", "Name")}

class SsmError(Exception): pass

class _Ssm:
    def __init__(self, ssm_client, instance_id, poll=1.5, timeout=90):
        self.ssm, self.iid, self.poll, self.timeout = ssm_client, instance_id, poll, timeout
    def run(self, doc, params):
        cid = self.ssm.send_command(InstanceIds=[self.iid], DocumentName=doc, Parameters=params, TimeoutSeconds=60)["Command"]["CommandId"]
        end = time.time() + self.timeout
        while time.time() < end:
            time.sleep(self.poll)
            try: inv = self.ssm.get_command_invocation(CommandId=cid, InstanceId=self.iid)
            except Exception as e:
                if "InvocationDoesNotExist" in type(e).__name__ or "InvocationDoesNotExist" in str(e): continue
                raise
            st = inv["Status"]
            if st == "Success": return parse_output(inv.get("StandardOutputContent", ""))
            if st in ("Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated"):
                raise SsmError(f"{doc} {st}: {(inv.get('StandardErrorContent') or '')[:200]}")
        raise SsmError(f"{doc} did not finish within {self.timeout}s")

def parse_output(stdout):
    lines = [l for l in stdout.strip().splitlines() if l.strip()]
    if not lines: raise SsmError("empty output from SSM document")
    try: return json.loads(lines[-1])
    except json.JSONDecodeError: raise SsmError(f"output was not JSON: {lines[-1][:120]}")

class AwsReadTarget:
    def __init__(self, session, region, instance_id):
        self.r = _Ssm(session.client("ssm", region_name=region), instance_id)
    def check_disk(self, drive="F:"): return self.r.run(READ_DOCS["check_disk"], {"Drive": [drive]})
    def check_cpu(self): return self.r.run(READ_DOCS["check_cpu"], {})
    def check_service(self, name): return self.r.run(READ_DOCS["check_service"], {"Name": [name]})
    def check_memory(self): return self.r.run(READ_DOCS["check_memory"], {})
    def read_events(self, log="System", last="20"): return self.r.run(READ_DOCS["read_events"], {"LogName": [log], "Last": [str(last)]})

class AwsActTarget:
    def __init__(self, session, region, instance_id):
        self.r = _Ssm(session.client("ssm", region_name=region), instance_id)
    def act(self, action, target):
        if action not in ACT_DOCS: raise ValueError(f"unknown action {action}")
        doc, param = ACT_DOCS[action]
        return self.r.run(doc, {param: [target]})

METRIC_KIND = {"DiskUsedPercent": "disk", "W3SVCRunning": "service", "CpuPercent": "cpu", "MemUsedPercent": "memory"}

def alarm_to_alert(a, instance_id):
    return {"name": a["AlarmName"], "description": a.get("AlarmDescription") or a["AlarmName"],
            "metric": METRIC_KIND.get(a.get("MetricName")), "instance": instance_id, "severity": "major"}

class AlarmPoller:
    """CloudWatch alarms are polled (no public URL needed). Each alarm state change is handled once."""
    def __init__(self, cw_client, instance_id, on_alert, prefix="Fury-", interval=30):
        self.cw, self.iid, self.on_alert, self.prefix, self.interval, self.seen = cw_client, instance_id, on_alert, prefix, interval, set()
        self.stop = threading.Event(); self.last_error = ""
    def poll_once(self):
        n = 0
        for a in self.cw.describe_alarms(AlarmNamePrefix=self.prefix, StateValue="ALARM").get("MetricAlarms", []):
            key = (a["AlarmName"], str(a.get("StateUpdatedTimestamp")))
            if key in self.seen: continue
            self.seen.add(key); self.on_alert(alarm_to_alert(a, self.iid)); n += 1
        return n
    def run_forever(self):
        while not self.stop.is_set():
            try: self.poll_once(); self.last_error = ""
            except Exception as e: self.last_error = f"{type(e).__name__}: {e}"
            self.stop.wait(self.interval)
