---
id: AWS-DISK-001
title: Data drive F: above 85%
kind: alert
applies_to: windows-ec2
symptoms: disk full, no space left on device, drive F usage, volume almost full, disk space
owner_team: SRE-Platform
escalate_to: SRE-Platform L2
version: 1.0
status: active
updated_at: 2026-11-18
---
# AWS-DISK-001: Data drive F: above 85%

## Steps
| # | type | step | tool / action | parameters | success criteria |
|---|---|---|---|---|---|
| 1 | check | Confirm usage on F: | check_disk | drive=F: | used_percent >= 85 confirms the issue |
| 2 | check | Look for disk errors | read_events | log=System, last=20 | disk-full events present |
| 3 | decide | Clearing temp is allowed only if most used space is in F:\fury-tmp. Otherwise escalate. | | | tmp_mb is more than half of used_mb |
| 4 | action | Clear temporary files (needs SRE Admin approval) | clear_temp_files | target=F:\fury-tmp | files deleted |
| 5 | check | Verify | check_disk | drive=F: | used_percent < 85 |
| 6 | escalate | Space is not in temp, or verification failed | escalate | team=SRE-Platform L2 | attach before and after evidence |

## Never
Do not delete anything outside F:\fury-tmp. Never touch C:.
