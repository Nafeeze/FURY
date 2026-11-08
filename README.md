# FURY V5: agent-to-agent SRE triage
agents: triage, knowledge, diagnostic (read-only), remediation (only one that can act), itsm (ServiceNow), comms (Slack)
Flow: CloudWatch alarm (polled) or engineer chat -> runbook match -> read-only checks -> ServiceNow incident -> Slack approval -> SSM action -> verify -> resolve or escalate to L2.
See SETUP.md. Offline: 27 tests, demo mode, compare.py.
