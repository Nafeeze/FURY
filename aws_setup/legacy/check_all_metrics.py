import boto3, datetime
cw = boto3.client("cloudwatch", region_name="ap-south-2")
for metric in ["DiskUsedPercent", "W3SVCRunning", "CpuPercent", "MemUsedPercent"]:
    r = cw.get_metric_statistics(Namespace="Fury", MetricName=metric, Dimensions=[{"Name":"InstanceId","Value":"i-05becfb96258d50e6"}], StartTime=datetime.datetime.utcnow()-datetime.timedelta(minutes=10), EndTime=datetime.datetime.utcnow(), Period=60, Statistics=["Maximum"])
    pts = sorted(r["Datapoints"], key=lambda x: x["Timestamp"])
    print(metric, "->", [(p["Timestamp"].strftime("%H:%M:%S"), p["Maximum"]) for p in pts])
