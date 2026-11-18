# FURY setup (do in this order)

## A. AWS (about 10 min, all in the browser)
1. Open the AWS console, region ap-south-2, click the CloudShell icon (>_) in the top bar.
2. CloudShell menu Actions > Upload file: upload `aws_setup/setup_aws.py`, `aws_setup/ec2_setup.ps1`, `aws_setup/faults.py` (same folder).
3. Run: `python3 setup_aws.py --region ap-south-2 --instance <your-instance-id>`
4. It prints two key pairs (shown once). Copy them into your laptop `.env`. Last line must show AWSPowerShell(Write-CWMetricData)=True.

## B. Slack (about 15 min)
App scopes (OAuth & Permissions > Bot Token Scopes): chat:write, channels:read, channels:history, groups:read, users:read, im:write
Event Subscriptions > Subscribe to bot events: message.channels   (typed yes/no replies in the approval thread)
Socket Mode ON (app-level token, scope connections:write, starts xapp-). Interactivity ON (no URL needed). Install to workspace (xoxb- token).
Create #sre-approvals and #sre-l2, then in each: /invite @YourBot

## C. ServiceNow
Group "SRE-Platform L2" must exist (User Administration > Groups > New). User fury_integration needs role itil, "Web service access only".

## D. Laptop
    pip install -r requirements.txt
    copy .env.example .env      (fill it in; never paste it into chat)
    python check_all.py         (every line must PASS)
    python run_fury.py          (open http://127.0.0.1:8080)
Try the UI with no external services first:  python run_fury.py --demo

## E. Demo
CloudShell: `python3 faults.py service_down`  -> alarm in ~2 min -> Slack approval request -> approve -> ticket resolved.
Also: disk_full, cpu_runaway, memory_pressure (no runbook, escalates), reset.
Week 5 comparison: `python compare.py`.   Tests: `python -m unittest tests.test_flow tests.test_adapters`

## F. Week 6: telemetry, measures, eval set
    python tests/eval_set.py                    # scenario + classifier + privacy eval, console report
    python tests/eval_set.py --json report.json # also write the full report to disk
    curl http://127.0.0.1:8080/api/metrics       # live counters/histograms while run_fury.py is running
Set `FURY_OTEL=on` (and `pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`) to ship real
OpenTelemetry spans/metrics instead of the built-in in-process recorder; `/api/metrics` works either way.
See `PRIVACY.md` for what's sent to the LLM, what's redacted, and how long chat context is kept.

## Mixing real and fake (2-hour mode)
In .env set any of FURY_SNOW / FURY_TARGET / FURY_CHAT to `fake` while the others are `real`. Example: real ServiceNow, fake server and chat:
    FURY_SNOW=real
    FURY_TARGET=fake      (UI shows buttons to break the fake server)
    FURY_CHAT=fake        (auto-approves after 5 seconds)

## G. Access control, audit and operations (required before any real deployment)
    python -m fury.auth new alice admin        # prints a token ONCE + the hashed entry
    # put the entry in .env:  FURY_API_TOKENS=alice:admin:<hash>   (comma-separate several users)
Roles: viewer = read-only, operator = viewer + chat/report, admin = everything (audit log, approval history, demo controls).
With real integrations FURY refuses to start without FURY_API_TOKENS (override: FURY_AUTH=off). The UI asks for the token on first use.
Endpoints (need a token): /api/health, /api/obs/slo, /api/audit (admin), /api/approvals/history (admin), /api/obs/session/<ref>.
Verify the audit log has not been edited:  python -c "from fury.audit import Audit; print(Audit.verify_file('audit.jsonl'))"
Durable state: FURY_DB=fury.db   Audit file: FURY_AUDIT_LOG=audit.jsonl   Retention: FURY_RETENTION_DAYS=90
