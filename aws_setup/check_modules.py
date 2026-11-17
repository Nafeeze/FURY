import boto3, time
ssm = boto3.client("ssm", region_name="ap-south-2")
cmd = """
Write-Output ("AWSCLI: " + [bool](Get-Command aws -ErrorAction SilentlyContinue))
Write-Output ("AWSPowerShell: " + [bool](Get-Module -ListAvailable AWSPowerShell))
Write-Output ("AWS.Tools.CloudWatch: " + [bool](Get-Module -ListAvailable AWS.Tools.CloudWatch))
"""
cid = ssm.send_command(InstanceIds=["i-05becfb96258d50e6"], DocumentName="AWS-RunPowerShellScript", Parameters={"commands":[cmd]})["Command"]["CommandId"]
time.sleep(5)
inv = ssm.get_command_invocation(CommandId=cid, InstanceId="i-05becfb96258d50e6")
print(inv["StandardOutputContent"])
print(inv["StandardErrorContent"])
