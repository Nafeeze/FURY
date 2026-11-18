# Runs ON the EC2 (via SSM, as SYSTEM). Idempotent. Sets up: IIS, F: data drive, fury-rogue/fury-leak binaries, metrics publisher.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force C:\fury | Out-Null

# 1. IIS (W3SVC is the service FURY protects)
if (-not (Get-WindowsFeature Web-Server).Installed) { Install-WindowsFeature Web-Server | Out-Null }
Set-Service W3SVC -StartupType Automatic; if ((Get-Service W3SVC).Status -ne 'Running') { Start-Service W3SVC }

# 1b. CloudWatch cmdlets for the metrics publisher. Amazon Windows AMIs usually ship the full AWSPowerShell module;
#     if neither module is present, install the modular one (needs outbound internet).
if (-not (Get-Module -ListAvailable -Name AWSPowerShell) -and -not (Get-Module -ListAvailable -Name AWS.Tools.CloudWatch)) {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force | Out-Null
  Install-Module -Name AWS.Tools.CloudWatch -Force -AllowClobber -Scope AllUsers | Out-Null
}

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
$ErrorActionPreference = 'Stop'
try {
if (Get-Module -ListAvailable -Name AWS.Tools.CloudWatch) { Import-Module AWS.Tools.CloudWatch } else { Import-Module AWSPowerShell }
function Put($name, $value) {
  $d = New-Object Amazon.CloudWatch.Model.Dimension; $d.Name = 'InstanceId'; $d.Value = $inst
  $m = New-Object Amazon.CloudWatch.Model.MetricDatum; $m.MetricName = $name; $m.Value = [double]$value; $m.Dimensions.Add($d); $m.Timestamp = (Get-Date).ToUniversalTime()
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
} catch { "$(Get-Date -Format s) $($_.Exception.Message)" | Add-Content C:ury\publish.log }
'@ | Set-Content C:\fury\publish_metrics.ps1 -Encoding UTF8

$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\fury\publish_metrics.ps1'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName FuryMetrics -Action $a -Trigger $t -User SYSTEM -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName FuryMetrics

$hasCw = [bool](Get-Module -ListAvailable -Name AWSPowerShell, AWS.Tools.CloudWatch)
Write-Output ("SETUP DONE | IIS=" + (Get-Service W3SVC).Status + " | F:=" + (Test-Path 'F:\fury-tmp') + " | AWSPowerShell(Write-CWMetricData)=" + $hasCw)
