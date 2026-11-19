"""FURY: ONE-FILE AWS SETUP.  Run it once, on a computer signed in to the AWS account as an administrator.

    pip install boto3
    python friend_setup.py            (add --yes to skip the confirmation question)

It will: show which account and server it is about to change and ask you to confirm; create the 8 FURY SSM documents,
2 least-privilege IAM users (one read-only, one for approved fixes) with their access keys, and 4 CloudWatch alarms;
install IIS, the F: data volume and the metrics publisher on the server; let the server's role publish the Fury metrics;
check the server's security groups; and save the new access keys to fury_aws_keys.env.

It does NOT change: anything outside account / server named below, existing users other than creating the two FURY users,
the server's network rules (it only reports them), or IMDS settings (unless you pass --harden-imds).
Safe to re-run: documents and alarms are updated in place, and a user that already has an access key is left alone.
"""
import argparse, json, sys, time
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    sys.exit("boto3 is not installed. Run:  pip install boto3   and try again.")

REGION, INSTANCE, EXPECTED_ACCOUNT = "ap-southeast-2", "i-0d7651de6a2954ff5", "640737808436"
KEYS = []

ap = argparse.ArgumentParser(description="FURY one-file AWS setup")
ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
ap.add_argument("--harden-imds", action="store_true", help="also require IMDSv2 on the server (test the metrics publisher afterwards)")
ap.add_argument("--region", default=REGION); ap.add_argument("--instance", default=INSTANCE)
args = ap.parse_args(); R, IID = args.region, args.instance

print("== Pre-flight")
try:
    sts = boto3.client("sts", region_name=R); ident = sts.get_caller_identity(); ACCOUNT = ident["Account"]
    ec2 = boto3.client("ec2", region_name=R); ssm, iam, cw = (boto3.client(s, region_name=R) for s in ("ssm", "iam", "cloudwatch"))
    inst = ec2.describe_instances(InstanceIds=[IID])["Reservations"][0]["Instances"][0]
except NoCredentialsError:
    sys.exit("No AWS credentials found. Sign in first (for example:  aws login   or   aws configure sso   or   aws configure ) and run again.")
except ClientError as e:
    sys.exit(f"AWS refused or could not find something: {e.response['Error']['Code']}. Check you are admin in the right account and that the server exists in {R}.")
name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "(no name)")
ping = (ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [IID]}])["InstanceInformationList"] or [{}])[0].get("PingStatus", "NOT REGISTERED")
print(f"  signed in as : {ident['Arn']}")
print(f"  account      : {ACCOUNT}" + ("" if ACCOUNT == EXPECTED_ACCOUNT else f"   <-- NOT the expected account {EXPECTED_ACCOUNT}"))
print(f"  server       : {IID}  '{name}'  {inst['InstanceType']}  state={inst['State']['Name']}  region={R}")
print(f"  Systems Manager status: {ping}")
if inst["State"]["Name"] != "running" or ping != "Online":
    sys.exit("The server must be running and 'Online' in Systems Manager (Fleet Manager). Fix that first, then run again.")
if ACCOUNT != EXPECTED_ACCOUNT and not args.yes:
    print("  The account differs from the one planned. Continue only if you are sure.")
if not args.yes and input("\nThis will change the account above. Type YES to continue: ").strip() != "YES":
    sys.exit("Cancelled. Nothing was changed.")

EC2_SCRIPT = r'''# Runs ON the EC2 (via SSM, as SYSTEM). Idempotent. Sets up: IIS, F: data drive, fury-rogue/fury-leak binaries, metrics publisher.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force C:\fury | Out-Null

# 1. IIS (W3SVC is the service FURY protects)
if (-not (Get-WindowsFeature Web-Server).Installed) { Install-WindowsFeature Web-Server | Out-Null }
Set-Service W3SVC -StartupType Automatic; if ((Get-Service W3SVC).Status -ne 'Running') { Start-Service W3SVC }

# 2. F: data drive as a 2 GB virtual disk (skip if you already attached an EBS volume as F:)
if (-not (Test-Path 'F:\')) {
  "create vdisk file=C:\fury\fury.vhdx maximum=2048 type=expandable`r`nselect vdisk file=C:\fury\fury.vhdx`r`nattach vdisk`r`ncreate partition primary`r`nformat fs=ntfs quick label=FURYDATA`r`nassign letter=F" | Set-Content C:\fury\make.txt -Encoding ASCII
  diskpart /s C:\fury\make.txt | Out-Null
}
"select vdisk file=C:\fury\fury.vhdx`r`nattach vdisk" | Set-Content C:\fury\attach.txt -Encoding ASCII
if (-not (Get-ScheduledTask -TaskName FuryAttachDisk -ErrorAction SilentlyContinue)) {
  Register-ScheduledTask -TaskName FuryAttachDisk -User SYSTEM -RunLevel Highest -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Action (New-ScheduledTaskAction -Execute 'diskpart.exe' -Argument '/s C:\fury\attach.txt') | Out-Null
}
New-Item -ItemType Directory -Force F:\fury-tmp | Out-Null

# 3. Renamed copies of powershell used by the fault injector (so the culprit has a recognisable name)
$ps = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
Copy-Item $ps C:\fury\fury-rogue.exe -Force
Copy-Item $ps C:\fury\fury-leak.exe -Force

# 4. Metrics publisher: custom CloudWatch metrics every minute (namespace Fury)
@'
$inst = '__INSTANCE__'; $region = '__REGION__'
Import-Module AWSPowerShell -ErrorAction SilentlyContinue
function Put($name, $value) {
  $d = New-Object Amazon.CloudWatch.Model.Dimension; $d.Name = 'InstanceId'; $d.Value = $inst
  $m = New-Object Amazon.CloudWatch.Model.MetricDatum; $m.MetricName = $name; $m.Value = [double]$value; $m.Dimensions = New-Object System.Collections.Generic.List[Amazon.CloudWatch.Model.Dimension]; $m.Dimensions.Add($d); $m.Timestamp = (Get-Date).ToUniversalTime()
  Write-CWMetricData -Namespace 'Fury' -MetricData $m -Region $region
}
$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='F:'"
if ($d) { Put 'DiskUsedPercent' ([math]::Round(100 * ($d.Size - $d.FreeSpace) / $d.Size, 1)) }
$s = Get-Service W3SVC -ErrorAction SilentlyContinue
Put 'W3SVCRunning' ([int]($s -and $s.Status -eq 'Running'))
$p = (Get-Counter '\Process(*)\% Processor Time' -SampleInterval 2 -MaxSamples 1).CounterSamples | Where-Object { $_.InstanceName -notin @('_total','idle') } | Sort-Object CookedValue -Descending | Select-Object -First 1
Put 'CpuPercent' ([math]::Min(100, [math]::Round($p.CookedValue, 1)))
$o = Get-CimInstance Win32_OperatingSystem
Put 'MemUsedPercent' ([math]::Round(100 * (1 - $o.FreePhysicalMemory / $o.TotalVisibleMemorySize), 1))
'@ | Set-Content C:\fury\publish_metrics.ps1 -Encoding UTF8

$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\fury\publish_metrics.ps1'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName FuryMetrics -Action $a -Trigger $t -User SYSTEM -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName FuryMetrics

$hasCw = [bool](Get-Command Write-CWMetricData -ErrorAction SilentlyContinue)
Write-Output ("SETUP DONE | IIS=" + (Get-Service W3SVC).Status + " | F:=" + (Test-Path 'F:\fury-tmp') + " | AWSPowerShell(Write-CWMetricData)=" + $hasCw)
'''

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
        KEYS.extend([f"{pre}_KEY_ID={k['AccessKeyId']}", f"{pre}_SECRET={k['SecretAccessKey']}"])
        print(f"  {user}: access key created (saved to the keys file at the end)")
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
script = EC2_SCRIPT.replace("__INSTANCE__", IID).replace("__REGION__", R)
cid = ssm.send_command(InstanceIds=[IID], DocumentName="AWS-RunPowerShellScript", Parameters={"commands": script.splitlines()}, TimeoutSeconds=600)["Command"]["CommandId"]
for _ in range(120):
    time.sleep(5)
    try: inv = ssm.get_command_invocation(CommandId=cid, InstanceId=IID)
    except ClientError: continue
    if inv["Status"] not in ("Pending", "InProgress", "Delayed"): break
print("  status:", inv["Status"]); print("  ", (inv.get("StandardOutputContent") or "").strip()[-400:]); print("  ", (inv.get("StandardErrorContent") or "").strip()[-400:])

print("\n== Extras")
prof = (inst.get("IamInstanceProfile") or {}).get("Arn")
if prof:
    try:
        role = iam.get_instance_profile(InstanceProfileName=prof.split("/")[-1])["InstanceProfile"]["Roles"][0]["RoleName"]
        iam.put_role_policy(RoleName=role, PolicyName="FuryPublishMetrics", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "Fury"}}}]}))
        print(f"  role {role}: may now publish metrics to the 'Fury' namespace only")
    except ClientError as e:
        print(f"  COULD NOT update the server role ({e.response['Error']['Code']}). Add cloudwatch:PutMetricData for namespace Fury to it by hand.")
else:
    print("  The server has no IAM role attached: attach one with Systems Manager access, then re-run.")
risky = []
for sg in ec2.describe_security_groups(GroupIds=[g["GroupId"] for g in inst["SecurityGroups"]])["SecurityGroups"]:
    rules = sg["IpPermissions"]
    print(f"  security group {sg['GroupId']}: {len(rules)} inbound rule(s)")
    for r in rules:
        for ip in r.get("IpRanges", []) + r.get("Ipv6Ranges", []):
            if ip.get("CidrIp") in ("0.0.0.0/0",) or ip.get("CidrIpv6") == "::/0":
                risky.append(f"{sg['GroupId']} port {r.get('FromPort', 'all')} open to the whole internet")
for x in risky: print("  WARNING:", x, " (FURY only needs Systems Manager; remove public inbound rules in the console)")
imds = inst.get("MetadataOptions", {}).get("HttpTokens", "optional")
if args.harden_imds:
    ec2.modify_instance_metadata_options(InstanceId=IID, HttpTokens="required", HttpEndpoint="enabled"); print("  IMDSv2 is now required on the server")
else:
    print(f"  IMDS tokens: {imds}" + ("  (run again with --harden-imds to require IMDSv2)" if imds != "required" else ""))

print("\n== Result")
if KEYS:
    with open("fury_aws_keys.env", "w", encoding="utf-8") as f: f.write("\n".join(KEYS) + "\n")
    print("  Saved the 4 new access keys to  fury_aws_keys.env  in this folder.")
    print("  Give those 4 lines to the FURY owner privately (copy them into their .env), then DELETE the file.")
else:
    print("  No new keys were created because both FURY users already have a key. To get fresh secrets: IAM > Users > fury-diagnostic / fury-remediation >")
    print("  Security credentials > delete the old key, then run this file again.")
print("  FURY owner next:  set FURY_TARGET=real in .env, then  python check_all.py")
print("  If the server line above said AWSPowerShell(Write-CWMetricData)=False, tell the FURY owner.")
