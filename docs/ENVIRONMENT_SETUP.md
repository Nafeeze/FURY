# FURY: New Environment Setup (AWS + ServiceNow + Slack)

Audience: whoever is building the demo environment from scratch (new AWS account, new ServiceNow
Personal Developer Instance). Follow the parts in order. Part 5 is a verification checklist: do not
move on until every line passes.

> **Status of this document:** written from the repo's own scripts (`aws_setup/setup_aws.py`,
> `aws_setup/ec2_setup.ps1`, `aws_setup/faults.py`, `check_all.py`, `fury/adapters.py`) and
> `SETUP.md`. It has **not** been executed against a brand-new account yet, so expect to hit the odd
> snag. Part 5 and the Troubleshooting table are there to catch them. Fix what you find and update this file.

**You can do FURY without any of this:** `python run_fury.py --demo` runs everything with fakes.
This guide is only for the live demo (real CloudWatch alarm, real ticket, real Slack approval).

---

## 0. What you are building

```
 EC2 (Windows, IIS + F: drive)  --metrics every 1 min-->  CloudWatch (namespace "Fury")
        ^                                                        |
        | SSM commands (read-only user / action user)            v
        |                                              4 alarms "Fury-*" (polled by FURY every 30s)
     FURY (your laptop: python run_fury.py)  ----->  ServiceNow PDI (tickets)
        |
        +-----> Slack (#sre-approvals) : human taps Approve before any fix runs
```

| Piece | Names that must match exactly |
|---|---|
| CloudWatch namespace | `Fury` (metrics `DiskUsedPercent`, `W3SVCRunning`, `CpuPercent`, `MemUsedPercent`) |
| Alarms | `Fury-Disk-F-High`, `Fury-W3SVC-Stopped`, `Fury-CPU-High`, `Fury-Mem-High` |
| SSM documents | `Fury-Check-*` (5, read-only), `Fury-Act-*` (3, actions) |
| IAM users | `fury-diagnostic` (read-only), `fury-remediation` (actions only) |
| ServiceNow group | `SRE-Platform L2` |
| ServiceNow user | `fury_integration` |
| Slack channels | `#sre-approvals`, `#sre-l2` |

Time estimate: AWS ~60-90 min, ServiceNow ~30 min, Slack ~15 min, verification ~30 min.

---

## 1. AWS

Default region in the scripts is **ap-south-2 (Hyderabad)**. You can use another region, but then
pass `--region` everywhere and set `AWS_REGION` in `.env`. Keep it the same in every step.

### 1.1 Account hygiene (5 min)
1. Sign in to the new AWS account as an admin user (not root for day-to-day work).
2. Turn on a **billing alert** (Billing > Budgets > e.g. USD 20/month). A Windows t3.medium left running is the main cost.
3. Pick the region (top-right of the console) and stay in it.

### 1.2 IAM role for the EC2 instance (10 min)
The instance needs two things from AWS: to be managed by Systems Manager (so FURY can run commands),
and to publish custom metrics to CloudWatch.

1. IAM > Roles > Create role > trusted entity **AWS service > EC2**.
2. Attach policies:
   - `AmazonSSMManagedInstanceCore`
   - `CloudWatchAgentServerPolicy` (includes `cloudwatch:PutMetricData`, which the metrics publisher uses)
3. Name it `FuryInstanceRole`.

### 1.3 Launch the EC2 instance (10 min)
1. EC2 > Launch instance.
   - **AMI:** Microsoft Windows Server 2022 Base (Amazon-provided; it ships the SSM agent and the AWS Tools for PowerShell).
   - **Type:** `t3.medium` (recommended). `t3.small` is the minimum: the `memory_pressure` fault needs the box to actually reach ~92% memory, and a Windows desktop OS idles at a fair amount.
   - **Key pair:** not required. FURY uses SSM, not RDP. Choose "Proceed without a key pair" unless you want RDP for debugging.
   - **Network:** default VPC with a **public IP** (or a NAT) so the SSM agent can reach AWS. **No inbound rules needed** (leave security group with no inbound; outbound all).
   - **IAM instance profile:** `FuryInstanceRole`.
   - **Storage:** default 30 GiB is fine. The setup script creates the `F:` drive itself as a 2 GB virtual disk, so you do not need a second EBS volume. (If you prefer a real EBS volume, attach a small one and assign it letter `F:` before running the setup; the script skips creating the virtual disk when `F:` already exists.)
2. Name the instance `fury-demo`. **Copy the instance ID** (looks like `i-0123456789abcdef0`). You will use it in nearly every later step.
3. Wait ~5 minutes, then check **Systems Manager > Fleet Manager**: the instance must appear as **Online**. If it does not after 10 minutes, see Troubleshooting.

### 1.4 Run the AWS setup script in CloudShell (15 min)
1. Console top bar > **CloudShell** (the `>_` icon). Make sure the region shown matches your region.
2. CloudShell > **Actions > Upload file**. Upload these three files from the repo's `aws_setup/` folder:
   - `setup_aws.py`
   - `ec2_setup.ps1`
   - `faults.py`

   They must all be in the same folder in CloudShell.
3. Run (replace the instance ID and region):
   ```
   python3 setup_aws.py --region ap-south-2 --instance i-0123456789abcdef0
   ```
4. It creates (and is safe to re-run):
   - 8 SSM documents (5 read-only `Fury-Check-*`, 3 actions `Fury-Act-*`)
   - 2 IAM users with separate least-privilege policies
   - 4 CloudWatch alarms
   - Configures the EC2: installs IIS (the `W3SVC` service FURY protects), creates the `F:` drive and `F:\fury-tmp`, creates the fault helper executables, and installs a scheduled task `FuryMetrics` that publishes metrics every minute
5. **Copy the two key pairs it prints.** They are shown **once**:
   ```
   AWS_DIAG_KEY_ID=...
   AWS_DIAG_SECRET=...
   AWS_REMED_KEY_ID=...
   AWS_REMED_SECRET=...
   ```
   Put them in a password manager now, then into your laptop `.env` (Part 4). Never paste them into chat, tickets, or git.
   If you lose them: IAM > Users > `fury-diagnostic` > Security credentials > delete the old key, then re-run the script to mint a new one (same for `fury-remediation`).
6. The last line of output must say `AWSPowerShell(Write-CWMetricData)=True`.
   - If it says `False`, the metrics publisher cannot publish. Fix it (see 1.5) before continuing.

### 1.5 Make sure metrics are really flowing (this was the weak spot before)
`ec2_setup.ps1` now installs the CloudWatch PowerShell module itself if it is missing, picks whichever module exists, and writes any publisher error to `C:\fury\publish.log` on the instance. The old patch scripts (`aws_setup/legacy/`) are no longer needed. **Do not use them.**

1. Wait 3 minutes, then: **CloudWatch > Metrics > All metrics > Fury > InstanceId**. You should see four metrics for YOUR instance ID (`W3SVCRunning`=1, `DiskUsedPercent`, `CpuPercent`, `MemUsedPercent`).
2. If the `Fury` namespace is missing or empty, run on the instance via Systems Manager > Run Command > `AWS-RunPowerShellScript`:
   - `Get-Content C:\fury\publish.log` : shows the publisher's last error. Typical causes:
     - *module not found*: `Install-PackageProvider NuGet -Force; Install-Module AWS.Tools.CloudWatch -Force -AllowClobber -Scope AllUsers`
     - *not authorized / AccessDenied*: the instance role (1.2) is not attached or lacks `cloudwatch:PutMetricData`.
   - `Get-Content C:\fury\publish_metrics.ps1 | Select -First 3` : confirm `$inst` and `$region` are YOUR values.
   - `Start-ScheduledTask FuryMetrics`, wait a minute, check again. `Get-ScheduledTask FuryMetrics` must exist.
3. Alarms (**CloudWatch > Alarms**) go `Insufficient data` > `OK` in a few minutes. They use `TreatMissingData=notBreaching`, so they also look `OK` when **no** metrics arrive. Do not trust `OK` alone: `python check_all.py` has a **"Metrics are FRESH for this instance"** line that fails if any of the 4 metrics has no datapoint in the last 6 minutes.

### 1.6 Prove the fault injector works (10 min)
Back in CloudShell (you are admin there; FURY's users cannot do this):
```
python3 faults.py service_down --region ap-south-2 --instance i-0123456789abcdef0
```
- Expect: `Success W3SVC stopped`, then within ~2 minutes `Fury-W3SVC-Stopped` turns `ALARM`.
- Reset: `python3 faults.py reset --region ... --instance ...`

Available faults: `disk_full`, `service_down`, `cpu_runaway`, `memory_pressure`, `reset`.
`memory_pressure` is intentionally one with **no runbook**, so FURY must escalate to L2 instead of fixing.

Note: `--instance` is required for `faults.py`.

---

## 2. ServiceNow Personal Developer Instance (PDI)

### 2.1 Get an instance (10 min)
1. Create a free account at **developer.servicenow.com**.
2. **Request an instance** (latest release is fine). When ready, you get the URL (`https://devXXXXX.service-now.com`), username `admin`, and an admin password. Save them.
3. **Important:** PDIs hibernate after inactivity and can be reclaimed if unused. Log in to the developer portal before the demo and click Wake up if needed. Log in at least weekly, and **do not wait until the morning of the demo**. This is likely why the previous instance went offline.

### 2.2 Create the integration user (5 min)
1. In the instance, **All > User Administration > Users > New**.
2. User ID: `fury_integration`. Set a strong password.
3. Tick **Web service access only** (stops it logging in to the UI).
4. Save, then in the **Roles** related list add role **`itil`**.
   - FURY needs to create, update, add work notes to, and resolve incidents, read the resolution-code choices, and read/create the assignment group.

### 2.3 Create the L2 group (3 min)
1. **All > User Administration > Groups > New**.
2. Name exactly: `SRE-Platform L2`. Save.
3. (Optional) add yourself as a member so escalated tickets are visible in your list.
   FURY looks the group up by this exact name; if it is missing, escalation tickets still get created but without assignment.

### 2.4 Find the resolution code value (5 min)
FURY must set a *Resolution code* when it resolves a ticket. Valid values vary by instance.
1. Open any incident > **Resolution Information** tab > look at the **Resolution code** dropdown.
2. Note the exact text of a sensible choice. Typically `Solved (Permanently)` (or `Solved (Work Around)`).
3. You will put it in `.env` as `SNOW_CLOSE_CODE`. (If unset, FURY tries to read the choices itself, and `check_all.py` tells you if that fails.)

### 2.5 Quick manual test with curl (optional, 3 min)
Replace host/password. A JSON list (even empty) means API access and role are fine; HTTP 401/403 means user or role problem.
```
curl -s -u fury_integration:'PASSWORD' -H "Accept: application/json" "https://devXXXXX.service-now.com/api/now/table/incident?sysparm_limit=1"
```

---

## 3. Slack (only if the old app is gone)

If the previous Slack app still exists in the workspace, reuse it and skip to the channels step; you only need the tokens.

1. api.slack.com/apps > **Create New App** (from scratch) > pick the workspace.
2. **OAuth & Permissions > Bot Token Scopes:** `chat:write`, `channels:read`, `channels:history`, `groups:read`, `users:read`, `im:write`.
3. **Event Subscriptions** (on) > Subscribe to bot events: `message.channels`.
4. **Socket Mode: ON.** Create an app-level token with scope `connections:write` (starts `xapp-`). This is `SLACK_APP_TOKEN`.
5. **Interactivity: ON** (no URL needed).
6. **Install to workspace.** Copy the Bot User OAuth token (starts `xoxb-`). This is `SLACK_BOT_TOKEN`.
7. Create channels `#sre-approvals` and `#sre-l2`, and in each run `/invite @YourBotName`.
8. Find the **approver's Slack member ID** (profile > ⋮ > Copy member ID, looks like `U0XXXXXXXX`). Only IDs listed in `SLACK_APPROVER_IDS` can approve a remediation. Use the person who plays "SRE Admin" in the demo, **not** the engineer who reports the problem.

---

## 4. Laptop `.env`

```
copy .env.example .env      (Windows)     # then edit .env. NEVER commit it or paste it anywhere.
pip install -r requirements.txt
```

Fill in:

| Key | Value |
|---|---|
| `SNOW_URL` | `https://devXXXXX.service-now.com` (no trailing slash needed) |
| `SNOW_USER` | `fury_integration` |
| `SNOW_PASSWORD` | the password you set in 2.2 |
| `SNOW_CLOSE_CODE` | the exact resolution-code text from 2.4 |
| `APPROVAL_SECRET` | a long random string. Generate with `python -c "import secrets;print(secrets.token_hex(32))"` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | from Part 3 |
| `SLACK_APPROVER_IDS` | comma-separated Slack member IDs |
| `AWS_REGION` | e.g. `ap-south-2` |
| `AWS_INSTANCE_ID` | your new instance ID (required for a real target) |
| `AWS_DIAG_KEY_ID`, `AWS_DIAG_SECRET` | from 1.4 |
| `AWS_REMED_KEY_ID`, `AWS_REMED_SECRET` | from 1.4 |
| `FURY_SNOW`, `FURY_TARGET`, `FURY_CHAT` | `real` for each piece that is set up; `fake` for any that is not yet |
| `FURY_DB` (optional) | `fury.db` to keep run history across restarts |
| `FURY_CHAT_MODE` (optional) | `full` to let *chat* open incidents. Default `readonly`. CloudWatch alarms always work regardless |

You can go piece by piece: e.g. real ServiceNow with `FURY_TARGET=fake` and `FURY_CHAT=fake` while AWS is still being built.

---

## 5. Verification checklist (do not skip)

Run from the repo folder on the laptop:
```
python check_all.py            # everything
python check_all.py --only snow     # or aws, slack
```
Every line must say `PASS`. It never prints secrets. It creates and resolves one throw-away test incident in ServiceNow.

What the lines mean and what to do when one fails:

| Line | Failing means |
|---|---|
| `.env has every key` | A key is missing/blank in `.env` |
| `ServiceNow: resolution code choices` | Set `SNOW_CLOSE_CODE` (step 2.4) |
| `ServiceNow: create + work note + resolve` | Wrong URL/user/password, user lacks `itil`, or instance is hibernating |
| `ServiceNow: L2 assignment group` | Group name not exactly `SRE-Platform L2` |
| `AWS identity` (diagnostic / remediation) | Access keys wrong, copied with spaces, or user deleted |
| `Metrics are FRESH for this instance` | Publisher failing or wrong instance ID/region: see 1.5 |
| `EC2 is Online in Systems Manager` | Instance role missing/wrong, no internet route, or wrong `AWS_INSTANCE_ID` / region |
| `Real read-only checks over SSM` | SSM docs not created, or IIS / `F:` not set up (re-run `setup_aws.py`) |
| `Least privilege: read-only user cannot act` | **Must PASS.** If it fails, IAM policies are wrong; re-run `setup_aws.py` |
| alarms | Fewer than 4 `Fury-` alarms, re-run `setup_aws.py` |
| Slack lines | Tokens wrong, bot not invited to channels, Socket Mode off |

Then the end-to-end demo:

1. `python run_fury.py` and open http://127.0.0.1:8080 (watch the console: it prints `snow=real server=real chat=real`).
2. In CloudShell: `python3 faults.py service_down --region ... --instance ...`
3. Within ~2-3 minutes: alarm > FURY triages > runbook matched > ServiceNow ticket created > Slack approval message in `#sre-approvals`.
4. The approver replies **yes/approve** in the thread. FURY restarts W3SVC, verifies it is healthy, and resolves the ticket.
5. Repeat with `disk_full` (clears `F:\fury-tmp`), `cpu_runaway` (kills the rogue process), and `memory_pressure` (**should escalate to L2**, not auto-fix).
6. `python3 faults.py reset ...` between runs.

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Instance never shows Online in Fleet Manager | IAM role not attached; no outbound internet (public IP / NAT / VPC endpoints for `ssm`, `ssmmessages`, `ec2messages`); wait 10 min after first boot |
| `setup_aws.py` AccessDenied | CloudShell user is not an admin of the account |
| `setup_aws.py` hangs on the EC2 step | Instance not Online in SSM yet, so wait and re-run (it is idempotent) |
| `AWSPowerShell(Write-CWMetricData)=False` or no `Fury` namespace | See 1.5 (read `C:\fury\publish.log`) |
| Alarms never fire | Metrics not flowing (1.5); fault did not apply (check `faults.py` output says `Success`) |
| Alarm fires but FURY ignores it | `AWS_INSTANCE_ID` / region mismatch with the alarm's dimension; alarm name must start with `Fury-`; check `FURY_TARGET=real` |
| Ticket not resolving | Wrong `SNOW_CLOSE_CODE` text; user lacks `itil` |
| ServiceNow 401/403 | Password, or "Web service access only" user missing `itil` |
| ServiceNow connection refused / timeout | PDI is hibernating. Wake it in the developer portal |
| Slack message appears but approving does nothing | Reply must come from an ID in `SLACK_APPROVER_IDS`; Event Subscriptions `message.channels` must be on; reply in the thread |
| Approval expires | Tokens expire after 10 min by default; re-trigger |
| Chat says "reporting is disabled in read-only mode" | Set `FURY_CHAT_MODE=full` (alarms are unaffected) |
| `memory_pressure` does nothing | Instance too small/too large for the fault to reach 92%; try t3.small, or adjust the loop in `faults.py` |

---

## 7. Cost and tear-down
- Stop (not terminate) the EC2 instance between demos; a stopped instance only costs for the volume. Metrics resume when it starts (the scheduled task is set to run every minute).
- To remove everything: delete the 4 alarms, 8 SSM documents (`Fury-*`), the 2 IAM users, the EC2 instance and `FuryInstanceRole`.
- Rotate or delete the IAM access keys when the project ends.

## 8. What changed in the code for this setup (for reviewers)
- `aws_setup/ec2_setup.ps1`: installs/imports the CloudWatch module itself and logs publisher errors to `C:\fury\publish.log`.
- `aws_setup/legacy/`: the old `check_*.py` / `fix_publish*.py` one-off scripts moved here (they hard-code an old instance ID). Not used.
- No hard-coded instance IDs in live code: `faults.py` requires `--instance`; `check_all.py` and `run_fury.py` require `AWS_INSTANCE_ID` for a real target; fake/demo mode uses `i-demo`.
- `check_all.py`: new "Metrics are FRESH for this instance" check; `AWS_INSTANCE_ID` is now a required key.
- Still open: not yet run against a brand-new AWS account or ServiceNow PDI. Update this guide with anything you hit.
