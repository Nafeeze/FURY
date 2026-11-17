# FURY V5: agent-to-agent SRE triage
agents: triage, knowledge, diagnostic (read-only), remediation (only one that can act), itsm (ServiceNow), comms (Slack)
Flow: CloudWatch alarm (polled) or engineer chat -> runbook match -> read-only checks -> ServiceNow incident -> Slack approval -> SSM action -> verify -> resolve or escalate to L2.
See SETUP.md. Offline: 27 tests, demo mode, compare.py.

## Observability dashboard (Week 6)

Start the app (`python run_fury.py --demo`), open http://127.0.0.1:8080 and click the **Observability** tab.

- **Signals that need a person**: each has an owner, a threshold and the action to take.
- **Requests (traces)**: one row per chat request or alarm. Click one to open its waterfall (chat.request > run > agent steps > retrieval, tool, decision, approval spans). Click a span for its attributes.
- **Agent step latency, outcomes, model use**: from the same recorder.
- **Evaluation set**: shows the last `python tests/eval_set.py` run (written to `reports/eval_latest.json`).

API behind it: `/api/obs/overview`, `/api/obs/trace/<trace_id>`, `/api/obs/eval`.
With `FURY_OTEL=on` and the SDK installed, the same spans are also exported through OpenTelemetry using the same trace IDs shown in the dashboard.
