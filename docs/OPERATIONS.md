# Operating FURY

How to run, watch, and recover FURY itself. Everything here is something the code does today; items that were written but never run are marked **UNVERIFIED**.

## 1. Start, stop, health
| Task | How |
|---|---|
| Start (all real) | `python run_fury.py` (UI at `http://127.0.0.1:8080`) |
| Start with a simulated server | `FURY_TARGET=fake` in `.env` (ServiceNow and Slack stay real) |
| Start fully simulated | `python run_fury.py --demo` |
| Production server | `pip install waitress`; FURY uses it automatically when present |
| Stop | Ctrl+C. Only one instance may run per port: a second one will share the port and answer inconsistently, so check first. |
| Health | `GET /api/health` (needs a token). Per-integration state: `ok`, `degraded`, `open` (circuit breaker tripped), `unmonitored`. |

Refuses to start with real integrations and no API token unless `FURY_AUTH=off` (trusted single-user machine only).

## 2. Configuration (`.env`)
| Key | Meaning |
|---|---|
| `SNOW_URL`, `SNOW_USER`, `SNOW_PASSWORD`, `SNOW_CLOSE_CODE` | ServiceNow instance, integration account, resolution code (e.g. `Solution provided`) |
| `AWS_REGION`, `AWS_INSTANCE_ID`, `AWS_DIAG_*`, `AWS_REMED_*` | The managed server and two separate least-privilege IAM identities (read vs act) |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_APPROVER_IDS`, `SLACK_APPROVALS_CHANNEL`, `SLACK_L2_CHANNEL`, `SLACK_OPS_CHANNEL` | Slack. Only listed approver IDs can approve a change. Ops channel receives SLO alerts (defaults to the L2 channel). |
| `APPROVAL_SECRET` | Signs approval tokens |
| `FURY_SNOW` / `FURY_TARGET` / `FURY_CHAT` | `real` or `fake`, per integration |
| `FURY_CHAT_MODE` | `readonly` (default: chat cannot open incidents) or `full` |
| `FURY_API_TOKENS` | `name:role:sha256` entries; roles `viewer`, `operator`, `admin` |
| `FURY_DB`, `FURY_AUDIT_LOG`, `FURY_RETENTION_DAYS` | Durable state, tamper-evident audit file, retention (default 90 days) |
| `FURY_HOST`, `FURY_PORT` | Bind address (default `127.0.0.1:8080`) |
| `FURY_SLO_ALERT_SECONDS` | SLO check interval (default 60; `0` disables alerts) |
| `FURY_LLM=on`, `NVIDIA_API_KEY`, `NVIDIA_MODEL`, `NVIDIA_FALLBACK_MODELS`, `NVIDIA_EMBED_MODEL` | The chat model. FURY tries the primary then the fallbacks and skips a retired model for 10 minutes. **Hosted models are retired without notice** (the original default died on 2026-11-18): if `/api/obs/overview` shows many `llm.model.error` or the chat sounds canned, list current models at `<NVIDIA_BASE_URL>/models` and update `NVIDIA_MODEL`. FURY always falls back to fixed text, never to silence. |
| `FURY_SEMANTIC=on`, `FURY_EMBED_MIN` | Optional semantic (embedding) search; keep off until the floor is calibrated against the golden set |
| `FURY_KB_REFRESH_SECONDS` | How often to ingest published ServiceNow knowledge articles (default 900; `0` disables) |
| `FURY_SECRETS_PROVIDER=aws`, `FURY_SECRETS_ID` | Load secrets from AWS Secrets Manager (**UNVERIFIED** against live AWS) |

`.env` is git-ignored. Never commit it, paste it in chat, or put real tokens in documentation.

## 3. Access tokens
- Create: `python -m fury.auth new <name> <viewer|operator|admin>`. The token is shown once; put the printed `name:role:hash` line in `FURY_API_TOKENS` (comma-separate several).
- Rotate or revoke: replace or remove that entry and restart. FURY stores only hashes.
- Too many wrong tokens from one client are throttled (HTTP 429 for about a minute). That is expected, not an outage.

## 4. What to watch
- **Slack ops channel:** one message when an SLO breaches and one when it recovers (not one per check).
- **UI > Insights:** run counts, resolve rate, latency, failed steps, per-request trace.
- **`GET /api/obs/slo`:** objective, measured value, and `ok` / `breach` / `no_data` for completion, escalation rate, hop latency, integration errors, contract violations. Thresholds are env-tunable (`FURY_SLO_*`).
- **`GET /api/audit`** (admin): every request, denial, approval and SLO alert, hash-chained. Verify integrity with the `verify` function in `fury/audit.py` and ship the file to write-once storage for real assurance (the chain detects tampering; it does not prevent it).

## 5. Failure runbook
| Symptom | Likely cause | Action |
|---|---|---|
| Incidents list empty or "ServiceNow is unavailable" | PDI hibernating/expired, wrong `SNOW_URL`, or password changed | Wake the instance at developer.servicenow.com, check the URL, run `python check_all.py --only snow` |
| `/api/health` shows ServiceNow `open` | 5 consecutive failures tripped the circuit breaker | Fix the cause; it retries after a 30 s cool-down. Writes are never retried, so nothing is duplicated. |
| Tickets skipped as "duplicate" | An unresolved incident with the same alarm and instance is open | Resolve or close it; Resolved/Closed/Canceled never count as open |
| Approval request seen in Slack but nothing happens after a restart | Pending approvals do not survive a restart; they expire and the incident is escalated | Re-raise the incident |
| 401 from the UI | Token missing or wrong | Enter a valid token in the sign-in box |
| 429 | Auth throttle | Wait about a minute |
| Cannot read work notes in the UI | Integration user lacks read access to the journal table | Run `servicenow_setup/fury_snow_acl.js` once as admin (creates one narrow read ACL: incident work notes only) |
| Chat answers are generic or canned | Model retired or API key invalid; FURY fell back to fixed text | See the `FURY_LLM` row above; check `GET /api/obs/overview` for `llm.model.error` |
| "How do I ..." gets no knowledge-base answer | No published KB articles, or ingestion cannot read them | `GET /api/kb` shows count and last error; seed samples with `servicenow_setup/fury_snow_kb_seed.js`; `POST /api/kb/refresh` (admin) forces a refresh |
| SLO breach message in Slack | See the message; open Insights to find the failing step | Escalation-rate breaches usually mean runbooks do not cover real incidents |

## 6. Backup and recovery
- State is the SQLite file at `FURY_DB` and the audit log at `FURY_AUDIT_LOG`.
- **Back up** (safe while FURY runs): `python -m fury.backup backup <dir>`. It uses SQLite's online backup, re-verifies the audit hash chain, and writes `manifest.json` with SHA-256 hashes.
- **Check a backup:** `python -m fury.backup verify <dir>` (hashes, audit chain, SQLite integrity).
- **Restore:** stop FURY, then `python -m fury.backup restore <dir> <new_db_path> <new_audit_path>` (refuses to overwrite without `--force`, verifies first, writes atomically). Point `FURY_DB` / `FURY_AUDIT_LOG` at the restored files and start FURY.
- The round trip (backup, tamper detection, restore into a fresh location) is covered by `tests/test_backup.py`. Schedule the backup (Task Scheduler / cron) and copy the folder off the machine.
- After a restart, interrupted runs are marked failed and escalated, and stale approvals expire. Workflows are **not** resumed.
- A full rebuild on a second machine has not been rehearsed end to end; the data-restore step has. Recreating FURY needs the repo, `.env` (or the Secrets Manager secret; **never** in the backup), and the restored SQLite/audit files if history matters.

## 7. TLS and exposure
FURY has no TLS of its own. Keep it on `127.0.0.1` and terminate TLS in front of it: `deploy/Caddyfile` is a ready reverse-proxy config, and `deploy/Dockerfile` builds a non-root container (**UNVERIFIED**: neither has been run by the author). The UI and API send `nosniff`, `X-Frame-Options: DENY`, a same-origin CSP and `Cache-Control: no-store` on API responses.

## 8. Capacity (measured, not promised)
`python tests/loadtest.py --clients 20 --seconds 10` drives the real app with fake backends: about 440 requests/s, p95 about 250 ms, zero 5xx errors on the author's laptop. This measures FURY itself only, not ServiceNow, AWS or Slack.

## 9. Releasing a change
`python -m unittest discover -s tests` must pass; run `python check_all.py` against the live systems; commit to a feature branch and open a pull request. `python -m pip_audit -r requirements.txt` reported no known vulnerabilities on the last run.
