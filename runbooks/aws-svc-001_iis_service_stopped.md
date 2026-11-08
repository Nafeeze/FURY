---
id: AWS-SVC-001
title: IIS service W3SVC stopped
kind: alert
applies_to: windows-ec2
symptoms: service stopped, W3SVC, IIS down, web service not running, website down, service not responding
owner_team: SRE-Platform
escalate_to: SRE-Platform L2
---
# AWS-SVC-001: IIS service W3SVC stopped

## Steps
| # | type | step | tool / action | parameters | success criteria |
|---|---|---|---|---|---|
| 1 | check | Check service state | check_service | name=W3SVC | state=stopped confirms the issue |
| 2 | check | Read why it stopped | read_events | log=System, last=20 | stop reason visible |
| 3 | decide | If events show disk-full errors, escalate (fix the disk first). Otherwise a restart is allowed. | | | no disk-full events |
| 4 | action | Restart the service (needs SRE Admin approval) | restart_service | target=W3SVC | service started |
| 5 | check | Verify | check_service | name=W3SVC | state=running |
| 6 | escalate | Restart did not bring it healthy | escalate | team=SRE-Platform L2 | attach before and after evidence |

## Never
At most one restart per incident. A second failure goes to L2.
