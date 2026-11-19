# FURY on AWS: CloudFormation stack

One template, `fury-aws-stack.yaml`, creates everything FURY needs in AWS for one Windows server. It replaces `aws_setup/friend_setup.py`: **use one or the other, never both** (they create the same names and would collide).

## What it creates
| Resource | Purpose |
|---|---|
| 8 SSM command documents `Fury-Check-*` (5, read-only) and `Fury-Act-*` (3, approved actions) | The only things FURY can run on the server. Identical to the ones in `aws_setup/setup_aws.py` |
| IAM users `fury-diagnostic` (read-only) and `fury-remediation` (approved actions) | Separate least-privilege identities, each limited to this instance and its own documents |
| Their access keys, stored in **Secrets Manager** (`fury/prod` by default) | Keys never appear in stack outputs or logs |
| 4 CloudWatch alarms `Fury-Disk-F-High`, `Fury-W3SVC-Stopped`, `Fury-CPU-High`, `Fury-Mem-High` | What FURY polls for. Names must stay `Fury-*` |
| SNS topic `fury-alerts` (+ optional email subscription) | Notifies people when an alarm fires or clears |
| Policy on the server's role: `cloudwatch:PutMetricData`, namespace `Fury` only | Lets the server publish the metrics the alarms watch |
| SSM association `fury-server-setup` (optional) | One-time server setup: IIS, F: data volume, metrics publisher |

Not in the template, on purpose: the EC2 instance itself and its role (you already have them), and any inbound network rules (FURY needs none).

## Before you deploy
- The server must be **running** and **Online** in Systems Manager (Fleet Manager). Its role needs `AmazonSSMManagedInstanceCore` (it already has it if it shows as Online).
- Know the **instance ID** and the **name of the instance's IAM role** (EC2 > the instance > Security tab > IAM role).
- Region: the one where the instance lives (Sydney, `ap-southeast-2`).
- Do **not** deploy if `friend_setup.py` or `setup_aws.py` was already run in this account: delete the `Fury-*` documents and alarms and the two `fury-*` users first, or just keep using that setup.

## Deploy: console
1. CloudFormation > **Create stack > With new resources** > **Upload a template file** > choose `fury-aws-stack.yaml`.
2. Stack name `fury-aws`. Fill in the parameters:
   - **InstanceId**: pick your server from the list.
   - **InstanceRoleName**: for example `FuryInstanceRole`.
   - **AlertEmail**: optional (you must click the confirmation link AWS emails you).
   - **ConfigureServer**: `true` for a fresh server; `false` if IIS, F: and the metrics publisher are already set up.
   - **SecretName**: leave `fury/prod`.
3. On the last page tick **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"**, then **Create stack**. It takes a few minutes.

## Deploy: command line
```
aws cloudformation deploy --template-file deploy/cloudformation/fury-aws-stack.yaml --stack-name fury-aws --region ap-southeast-2 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides InstanceId=<i-...> InstanceRoleName=<role-name> AlertEmail=<optional> ConfigureServer=true
```

## After it succeeds
1. **Check the server setup finished** (only when `ConfigureServer=true`): run the command shown in the stack's **Outputs > ServerSetupCheck**. It should print `Success`.
2. **Get the keys** (run on a trusted machine, do not paste them in chat): the command is in **Outputs > HowToGetTheKeys**:
   ```
   aws secretsmanager get-secret-value --secret-id fury/prod --region ap-southeast-2 --query SecretString --output text
   ```
   It prints four values. Put them in FURY's `.env` as `AWS_DIAG_KEY_ID`, `AWS_DIAG_SECRET`, `AWS_REMED_KEY_ID`, `AWS_REMED_SECRET`.
3. In FURY's `.env` also set `AWS_REGION=ap-southeast-2`, `AWS_INSTANCE_ID=<the instance id>` and `FURY_TARGET=real`.
4. Run `python check_all.py`: the AWS lines should pass, including the check that the read-only user is *denied* an action.
5. Confirm the SNS subscription email if you gave one.

## Update, redeploy, remove
- **Change a parameter or the template:** deploy again with the same stack name; CloudFormation applies only the difference.
- **Rotate the keys:** change `DiagnosticKey` / `RemediationKey` (or create new keys in IAM, delete the old ones, and update the secret); then update FURY's `.env`.
- **Remove everything:** delete the stack. This deletes the documents, alarms, topic, both users, their keys and the secret, and detaches the metrics policy from the server's role. The instance and its role are untouched.

## If a deploy fails (the stack rolls back and removes what it made)
| Error mentions | Cause and fix |
|---|---|
| `already exists` (document, user, alarm, topic) | Something with that name exists from an earlier setup. Delete it, or keep the earlier setup and do not deploy this stack |
| `Role ... not found` / `NoSuchEntity` on the metrics policy | `InstanceRoleName` is wrong: copy it exactly from the instance's Security tab |
| `InvalidInstanceId` on `ServerSetup` | The instance is not Online in Systems Manager. Fix that, or deploy with `ConfigureServer=false` and run the server setup later |
| `Requires capabilities : [CAPABILITY_NAMED_IAM]` | Tick the acknowledgement (console) or add `--capabilities CAPABILITY_NAMED_IAM` |
| `AccessDenied` creating IAM resources | The deploying user needs IAM and Secrets Manager permissions (AdministratorAccess is simplest for the one-time deploy) |

## Notes
- The template was checked with `cfn-lint` (no errors or warnings). It has not been deployed by the author: if anything differs, send the stack's **Events** tab.
- The SSM documents are generated from the same source as `aws_setup/setup_aws.py`. If you change a document there, regenerate or edit this template so they stay the same.
- The server-setup script creates the F: drive as a 2 GB virtual disk on C: (the instance needs about 3 GB free) unless an F: volume already exists.
