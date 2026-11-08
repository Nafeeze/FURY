"""Run ONCE in AWS CloudShell (console top bar, the >_ icon). It uses your console admin login, so no keys are typed anywhere.
   python3 setup_aws.py --region ap-south-2 --instance i-00654e3403673c840
Creates: 8 SSM documents (5 read-only checks, 3 approved actions), 2 IAM users with separate least-privilege policies,
4 CloudWatch alarms, and configures the EC2 (IIS, F: drive, metrics publisher)."""
import argparse, json, time, pathlib, boto3
from botocore.exceptions import ClientError

ap = argparse.ArgumentParser(); ap.add_argument("--region", required=True); ap.add_argument("--instance", required=True)
a = ap.parse_args(); R, IID = a.region, a.instance
ssm, iam, cw, sts = (boto3.client(s, region_name=R) for s in ("ssm", "iam", "cloudwatch", "sts"))
ACCOUNT = sts.get_caller_identity()["Account"]

def doc(desc, params, script):
    return json.dumps({"schemaVersion": "2.2", "description": desc, "parameters": params,
                       "mainSteps": [{"action": "aws:runPowerShellScript", "name": "run", "inputs": {"runCommand": script.strip().splitlines()}}]})
P = lambda **kw: {"type": "String", "description": kw.pop("d", "param"), **kw}
J = " | ConvertTo-Json -Compress"
DOCS = {
 "Fury-Check-Disk": doc("Read-only: disk usage", {"Drive": P(allowedPattern="^[A-Z]:$", default="F:")}, """
$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='{{ Drive }}'"
if (-not $d) { Write-Output '{"error":"drive not found","healthy":false}'; exit 0 }
$pct = [math]::Round(100 * ($d.Size - $d.FreeSpace) / $d.Size, 1)
$used = [math]::Round(($d.Size - $d.FreeSpace) / 1MB)
$tmp = 0
if (Test-Path '{{ Drive }}\\fury-tmp') { $tmp = [math]::Round(((Get-ChildItem '{{ Drive }}\\fury-tmp' -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB) }
@{drive='{{ Drive }}'; used_percent=$pct; used_mb=$used; tmp_mb=$tmp; healthy=($pct -lt 85)}""" + J),
 "Fury-Check-Cpu": doc("Read-only: busiest process", {}, """
$s = (Get-Counter '\\Process(*)\\% Processor Time' -SampleInterval 2 -MaxSamples 1).CounterSamples | Where-Object { $_.InstanceName -notin @('_total','idle') } | Sort-Object CookedValue -Descending | Select-Object -First 1
$cpu = [math]::Min(100, [math]::Round($s.CookedValue, 1))
@{top_process=$s.InstanceName; top_cpu_percent=$cpu; healthy=($cpu -lt 80)}""" + J),
 "Fury-Check-Service": doc("Read-only: service state", {"Name": P(allowedValues=["W3SVC"], default="W3SVC")}, """
$s = Get-Service -Name '{{ Name }}' -ErrorAction SilentlyContinue
if (-not $s) { Write-Output '{"state":"unknown","healthy":false}'; exit 0 }
@{service='{{ Name }}'; state=$s.Status.ToString().ToLower(); healthy=($s.Status -eq 'Running')}""" + J),
 "Fury-Check-Memory": doc("Read-only: memory use", {}, """
$o = Get-CimInstance Win32_OperatingSystem
$u = [math]::Round(100 * (1 - $o.FreePhysicalMemory / $o.TotalVisibleMemorySize), 1)
@{used_percent=$u; healthy=($u -lt 90)}""" + J),
 "Fury-Check-Events": doc("Read-only: recent event log lines", {"LogName": P(allowedValues=["System", "Application"], default="System"), "Last": P(allowedPattern="^[0-9]{1,2}$", default="20")}, """
$ev = @(Get-WinEvent -LogName '{{ LogName }}' -MaxEvents {{ Last }} -ErrorAction SilentlyContinue | ForEach-Object { "$($_.ProviderName): $(($_.Message -split "`n")[0]) (Event $($_.Id))" })
ConvertTo-Json -Compress -InputObject @{log='{{ LogName }}'; events=$ev; healthy=$true}"""),
 "Fury-Act-RestartService": doc("ACTION: restart an allowlisted service", {"Name": P(allowedValues=["W3SVC"])}, """
Restart-Service -Name '{{ Name }}' -Force
Start-Sleep -Seconds 3
$s = Get-Service '{{ Name }}'
@{restarted='{{ Name }}'; state=$s.Status.ToString().ToLower()}""" + J),
 "Fury-Act-ClearTemp": doc("ACTION: clear F:\\fury-tmp only", {"Path": P(allowedValues=["F:\\fury-tmp"])}, """
$p = '{{ Path }}'
if ($p -ne 'F:\\fury-tmp') { throw 'path not allowed' }
$f = @(Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue)
$mb = [math]::Round((($f | Measure-Object Length -Sum).Sum) / 1MB)
$f | Remove-Item -Force -ErrorAction SilentlyContinue
@{deleted_files=$f.Count; freed_mb=$mb; scope=$p}""" + J),
 "Fury-Act-KillProcess": doc("ACTION: stop the allowlisted runaway process", {"Name": P(allowedValues=["fury-rogue"])}, """
$n = '{{ Name }}'
$was = [bool](Get-Process -Name $n -ErrorAction SilentlyContinue)
Stop-Process -Name $n -Force -ErrorAction SilentlyContinue
@{stopped=$n; was_running=$was}""" + J),
}
print("== SSM documents")
for name, body in DOCS.items():
    try: ssm.create_document(Content=body, Name=name, DocumentType="Command", DocumentFormat="JSON"); print("  created", name)
    except ssm.exceptions.DocumentAlreadyExists:
        v = ssm.update_document(Content=body, Name=name, DocumentVersion="$LATEST", DocumentFormat="JSON")["DocumentDescription"]["DocumentVersion"]
        ssm.update_document_default_version(Name=name, DocumentVersion=v); print("  updated", name)

inst_arn = f"arn:aws:ec2:{R}:{ACCOUNT}:instance/{IID}"
def pol(doc_prefix, extra=()):
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "ssm:SendCommand", "Resource": [inst_arn, f"arn:aws:ssm:{R}:{ACCOUNT}:document/{doc_prefix}*"]},
        {"Effect": "Allow", "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:DescribeInstanceInformation"], "Resource": "*"},
        *extra]})
cw_read = {"Effect": "Allow", "Action": ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics"], "Resource": "*"}
print("== IAM users (separate privileges)")
for user, body in (("fury-diagnostic", pol("Fury-Check-", [cw_read])), ("fury-remediation", pol("Fury-Act-"))):
    try: iam.create_user(UserName=user)
    except iam.exceptions.EntityAlreadyExistsException: pass
    iam.put_user_policy(UserName=user, PolicyName=user + "-policy", PolicyDocument=body)
    if not iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
        k = iam.create_access_key(UserName=user)["AccessKey"]
        pre = "AWS_DIAG" if user == "fury-diagnostic" else "AWS_REMED"
        print(f"  {user}: copy these two lines into your laptop .env (shown only once):\n    {pre}_KEY_ID={k['AccessKeyId']}\n    {pre}_SECRET={k['SecretAccessKey']}")
    else: print(f"  {user}: already has an access key. To get a new secret, delete the old key in IAM -> Users -> {user} -> Security credentials, then rerun.")

print("== CloudWatch alarms")
ALARMS = [("Fury-Disk-F-High", "DiskUsedPercent", "GreaterThanThreshold", 90, "Disk F: usage above 90%", 1),
          ("Fury-W3SVC-Stopped", "W3SVCRunning", "LessThanThreshold", 1, "W3SVC service stopped", 1),
          ("Fury-CPU-High", "CpuPercent", "GreaterThanThreshold", 90, "CPU utilization above 90%", 2),
          ("Fury-Mem-High", "MemUsedPercent", "GreaterThanThreshold", 90, "Memory usage above 90%", 2)]
for n, m, op, thr, desc, ev in ALARMS:
    cw.put_metric_alarm(AlarmName=n, AlarmDescription=desc, Namespace="Fury", MetricName=m, Dimensions=[{"Name": "InstanceId", "Value": IID}],
                        Statistic="Maximum" if op.startswith("Greater") else "Minimum", Period=60, EvaluationPeriods=ev, DatapointsToAlarm=ev,
                        Threshold=thr, ComparisonOperator=op, TreatMissingData="notBreaching"); print("  alarm", n)

print("== Configuring the EC2 (IIS, F: drive, metrics publisher). This takes 1 to 3 minutes...")
script = (pathlib.Path(__file__).parent / "ec2_setup.ps1").read_text().replace("__INSTANCE__", IID).replace("__REGION__", R)
cid = ssm.send_command(InstanceIds=[IID], DocumentName="AWS-RunPowerShellScript", Parameters={"commands": script.splitlines()}, TimeoutSeconds=600)["Command"]["CommandId"]
for _ in range(120):
    time.sleep(5)
    try: inv = ssm.get_command_invocation(CommandId=cid, InstanceId=IID)
    except ClientError: continue
    if inv["Status"] not in ("Pending", "InProgress", "Delayed"): break
print("  status:", inv["Status"]); print("  ", (inv.get("StandardOutputContent") or "").strip()[-400:]); print("  ", (inv.get("StandardErrorContent") or "").strip()[-400:])
print("\nDONE. If the line above says AWSPowerShell(Write-CWMetricData)=False, tell Claude before continuing.")
