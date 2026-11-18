"""Break the EC2 on purpose (run in CloudShell, as YOU; the agents have no access to this).
   python3 faults.py disk_full | service_down | cpu_runaway | memory_pressure | reset  --instance i-... [--region ap-south-2]"""
import sys, time, argparse, boto3
ap = argparse.ArgumentParser(); ap.add_argument("fault"); ap.add_argument("--region", default="ap-south-2"); ap.add_argument("--instance", required=True, help="EC2 instance id, e.g. i-0123456789abcdef0")
a = ap.parse_args()
S = {
 "disk_full": r"""$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='F:'"
$need = [long](0.94 * $d.Size - ($d.Size - $d.FreeSpace)); $i = 0
while ($need -gt 0) { $sz = [long][math]::Min($need, 400MB); fsutil file createnew "F:\fury-tmp\batch_export_$i.tmp" $sz | Out-Null; $need -= $sz; $i++ }
Write-Output "filled F:"
""",
 "service_down": "Stop-Service W3SVC -Force; Write-Output 'W3SVC stopped'",
 "cpu_runaway": "Start-Process C:\\fury\\fury-rogue.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','$e=(Get-Date).AddMinutes(20); while((Get-Date) -lt $e){}'; Write-Output 'fury-rogue started (self-stops in 20 min)'",
 "memory_pressure": "Start-Process C:\\fury\\fury-leak.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','$l=New-Object System.Collections.ArrayList; for($i=0;$i -lt 400;$i++){ $o=Get-CimInstance Win32_OperatingSystem; if(100*(1-$o.FreePhysicalMemory/$o.TotalVisibleMemorySize) -ge 92){break}; [void]$l.Add((\"x\"*20MB)+$i) }; Start-Sleep 600'; Write-Output 'fury-leak started (releases in 10 min). Use a t3.small or larger.'",
 "reset": r"""Start-Service W3SVC -ErrorAction SilentlyContinue
Stop-Process -Name fury-rogue,fury-leak -Force -ErrorAction SilentlyContinue
Remove-Item F:\fury-tmp\* -Force -ErrorAction SilentlyContinue
Write-Output "reset done"
""",
}
if a.fault not in S: sys.exit("choose: " + ", ".join(S))
ssm = boto3.client("ssm", region_name=a.region)
cid = ssm.send_command(InstanceIds=[a.instance], DocumentName="AWS-RunPowerShellScript", Parameters={"commands": S[a.fault].splitlines()})["Command"]["CommandId"]
for _ in range(60):
    time.sleep(3)
    try: inv = ssm.get_command_invocation(CommandId=cid, InstanceId=a.instance)
    except Exception: continue
    if inv["Status"] not in ("Pending", "InProgress", "Delayed"): break
print(inv["Status"], (inv.get("StandardOutputContent") or "").strip(), (inv.get("StandardErrorContent") or "").strip()[-300:])
