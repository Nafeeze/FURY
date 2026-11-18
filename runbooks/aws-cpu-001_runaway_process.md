---
id: AWS-CPU-001
title: Runaway process consuming CPU
kind: alert
applies_to: windows-ec2
symptoms: high cpu, cpu utilization, slow responses, runaway process, process stuck at 100 percent
owner_team: SRE-Platform
escalate_to: SRE-Platform L2
version: 1.0
status: active
updated_at: 2026-11-18
---
# AWS-CPU-001: Runaway process consuming CPU

## Steps
| # | type | step | tool / action | parameters | success criteria |
|---|---|---|---|---|---|
| 1 | check | Identify the busy process | check_cpu | | a process at >= 80% CPU |
| 2 | decide | Stopping is allowed only if the culprit is fury-rogue. Never stop system processes or the web service. | | | culprit is fury-rogue |
| 3 | action | Stop the runaway process (needs SRE Admin approval) | kill_process | target=fury-rogue | process stopped |
| 4 | check | Verify | check_cpu | | no process >= 80% |
| 5 | escalate | Culprit not allowlisted, or verification failed | escalate | team=SRE-Platform L2 | attach before and after evidence |

## Never
Only fury-rogue may be stopped.
