import boto3, time
ssm = boto3.client("ssm", region_name="ap-south-2")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\fury\\publish_metrics.ps1"
cid = ssm.send_command(InstanceIds=["i-05becfb96258d50e6"], DocumentName="AWS-RunPowerShellScript", Parameters={"commands":[cmd]})["Command"]["CommandId"]
time.sleep(6)
inv = ssm.get_command_invocation(CommandId=cid, InstanceId="i-05becfb96258d50e6")
print("STATUS:", inv["Status"])
print("STDOUT:", inv["StandardOutputContent"])
print("STDERR:", inv["StandardErrorContent"])

cw = boto3.client("cloudwatch", region_name="ap-south-2")
r = cw.list_metrics(Namespace="Fury")
print("\nMetrics currently registered in Fury namespace:")
for m in r["Metrics"]:
    print(" ", m["MetricName"], m["Dimensions"])
