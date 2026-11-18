import boto3, time
ssm = boto3.client("ssm", region_name="ap-south-2")
cmd = """
$t = Get-ScheduledTask -TaskName FuryMetrics -ErrorAction SilentlyContinue
if ($t) {
  $i = Get-ScheduledTaskInfo -TaskName FuryMetrics
  Write-Output ("State=" + $t.State + " LastRunTime=" + $i.LastRunTime + " LastTaskResult=" + $i.LastTaskResult + " NextRunTime=" + $i.NextRunTime)
} else {
  Write-Output "TASK NOT FOUND"
}
"""
cid = ssm.send_command(InstanceIds=["i-05becfb96258d50e6"], DocumentName="AWS-RunPowerShellScript", Parameters={"commands":[cmd]})["Command"]["CommandId"]
time.sleep(5)
inv = ssm.get_command_invocation(CommandId=cid, InstanceId="i-05becfb96258d50e6")
print("STDOUT:", inv["StandardOutputContent"])
print("STDERR:", inv["StandardErrorContent"])
