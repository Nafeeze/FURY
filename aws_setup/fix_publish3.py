import boto3, time
ssm = boto3.client("ssm", region_name="ap-south-2")
cmd = """
$content = @"
`$inst = "i-05becfb96258d50e6"; `$region = "ap-south-2"
Import-Module AWS.Tools.CloudWatch -ErrorAction Stop
function Put(`$name, `$value) {
  `$d = New-Object Amazon.CloudWatch.Model.Dimension; `$d.Name = "InstanceId"; `$d.Value = `$inst
  `$m = New-Object Amazon.CloudWatch.Model.MetricDatum
  `$m.MetricName = `$name; `$m.Value = [double]`$value; `$m.Dimensions = @(`$d); `$m.Timestamp = (Get-Date).ToUniversalTime()
  Write-CWMetricData -Namespace "Fury" -MetricData `$m -Region `$region
}
`$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID=`'F:`'"
if (`$d) { Put "DiskUsedPercent" ([math]::Round(100 * (`$d.Size - `$d.FreeSpace) / `$d.Size, 1)) }
`$s = Get-Service W3SVC -ErrorAction SilentlyContinue
Put "W3SVCRunning" ([int](`$s -and `$s.Status -eq "Running"))
`$p = (Get-Counter `'\\Process(*)\\% Processor Time`' -SampleInterval 2 -MaxSamples 1).CounterSamples | Where-Object { `$_.InstanceName -notin @("_total","idle") } | Sort-Object CookedValue -Descending | Select-Object -First 1
Put "CpuPercent" ([math]::Min(100, [math]::Round(`$p.CookedValue, 1)))
`$o = Get-CimInstance Win32_OperatingSystem
Put "MemUsedPercent" ([math]::Round(100 * (1 - `$o.FreePhysicalMemory / `$o.TotalVisibleMemorySize), 1))
"@
Set-Content -Path C:\\fury\\publish_metrics.ps1 -Value $content -Encoding UTF8
Write-Output "--- file rewritten, running it now ---"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\fury\\publish_metrics.ps1
Write-Output "--- done ---"
"""
cid = ssm.send_command(InstanceIds=["i-05becfb96258d50e6"], DocumentName="AWS-RunPowerShellScript", Parameters={"commands":[cmd]}, TimeoutSeconds=60)["Command"]["CommandId"]
time.sleep(10)
inv = ssm.get_command_invocation(CommandId=cid, InstanceId="i-05becfb96258d50e6")
print("STATUS:", inv["Status"])
print("STDOUT:", inv["StandardOutputContent"])
print("STDERR:", inv["StandardErrorContent"])
