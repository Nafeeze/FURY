# FURY: Enterprise Readiness Status

Honest status against the Architecture and Engineering Blueprint v1.0. "Verified" means covered by automated tests that
ran green in this repo (`python -m unittest discover -s tests`) and, where noted, a real server run. **Live verification as of 2026-11-19:**
ServiceNow (create, work note, resolve, assign, recurrence handling) and Slack (approval post, approver check, Socket Mode, a real
approve-and-resolve run) were exercised against real accounts. **AWS has not been verified live**: the server side was simulated
because no AWS credentials were available. Everything marked UNVERIFIED below was written and unit-tested but never run for real.

## Phase scorecard

| Phase | Blueprint goal | State | What exists (verified) | What is NOT done |
|---|---|---|---|---|
| 0 | Assessment and baseline | Done | `docs/PHASE_0_ASSESSMENT.md` | none |
| 1 | Voice foundation | **Deferred** | Chat channel hardened: read-only by default, one in-flight request per session, idle reset, session-to-trace correlation | **Voice itself.** Needs GPT-Live-1 access and a WebRTC session endpoint; decision taken to stay chat-only until then |
| 2 | Durable orchestration | Mostly done | SQLite durable runs, restart recovery (interrupted runs marked failed, pending approvals expired), per-workflow hop/time/agent budgets, concurrent workflows safe | Workflow **resume** after a restart (interrupted work is failed and escalated, not continued), LangGraph (not adopted: the existing bus already does bounded, observable delegation; migration not justified yet), Postgres |
| 3 | Specialized agents | Done for current scope | Six agents with distinct, enforced roles; versioned input contracts validated before any agent runs; structured, timestamped evidence | Output schemas; model-assisted reasoning agents (everything is deterministic by design today) |
| 4 | Intelligence and RAG | Foundation only | Runbook versioning and lifecycle (active/draft/retired), citations with version, governed retrieval (BM25 + fuzzy, RRF, access and status filters before ranking, abstains without evidence, staleness flag), golden set (recall@3 1.0, MRR 1.0, 17 queries, 4 unsupported queries correctly empty) | Embeddings and pgvector, ServiceNow knowledge ingestion, freshness reconciliation jobs, a larger golden set from real incidents. The current golden set is small and author-written: treat it as a regression baseline, not proof of real-world accuracy |
| 5 | Enterprise integrations | ServiceNow and Slack **verified live**; AWS **unverified** | Real ServiceNow, AWS (read/act IAM split) and Slack adapters; resilience wrapper (read retries, never retry writes, circuit breaker); `/api/health`; AWS/ServiceNow setup guide and scripts (branch `feature/aws-env-setup`) | **Live AWS run** (no credentials yet); MCP (direct API clients used instead); rate limiting |
| 6 | Governed autonomy | Done for current actions | Risk tiers with deny-by-default (`fury/policy.py`), human approval with signed expiring tokens bound to action/target/ticket, durable audit trail of every request and outcome, one-attempt-then-escalate that survives restarts | Approval via a UI/Slack interactive buttons with identity from the IdP; richer risk scoring |
| 7 | Operational excellence | Done for current scope | SLOs and `/api/obs/slo`, tamper-evident hash-chained audit log, retention pruning, health endpoint, traces/metrics/dashboard | Durable trace storage; a load test against the live integrations; a tested restore from backup |
| 8 | Enterprise hardening | Partly done | Token auth with three roles, deny-by-default route map, hashed tokens, throttling, audited denials, refuses to start unauthenticated with real integrations | **SSO/OIDC**, an independent penetration test, a disaster-recovery test, and running the TLS proxy/container files (`deploy/`) and the Secrets Manager loader against real infrastructure (all UNVERIFIED) |

## Test evidence
Run `python -m unittest discover -s tests`. Suites: flow, adapters, observability, chat policy, store, runbook provenance,
session/trace, budgets, policy and audit, contracts, retrieval (golden set), resilience, ops (audit/SLO/retention), auth.
Real-server checks performed: restart persistence of runs; authenticated access (401/200), incident run, SLO/health/audit
endpoints and audit-chain verification.

## What blocks "enterprise ready" today (in priority order)
1. **Live verification** of ServiceNow, AWS and Slack integrations (needs the accounts; see `docs/ENVIRONMENT_SETUP.md`).
2. **SSO and a secrets manager** instead of static tokens and `.env`.
3. **Independent security review / pen test** of the API and the remediation path.
4. **Real incident data** to grow the retrieval golden set and to calibrate SLO thresholds.
5. **Voice** (optional) once GPT-Live-1 access exists.

## Known limitations worth stating plainly
- Budgets and limits are cooperative: checked between steps, they never kill a step that is mid-flight (deliberate: abandoning a half-done action is less safe).
- The audit log detects tampering; it does not prevent it. Ship it to write-once storage for real assurance.
- Telemetry is in memory (newest 2000 spans) and clears on restart.
- Pending Slack approvals do not survive a restart; they are expired and the incident must be re-raised.

## Added 2026-11-19
- ServiceNow: a recurrence after resolution now opens a **new** ticket (Resolved/Closed/Canceled no longer count as open); FURY claims each ticket (In Progress, assigned to the integration user) and hands over to the L2 group on escalation. Verified live.
- Operations: Slack alerts on SLO breach/recovery (`fury/slo_alert.py`), `docs/OPERATIONS.md` (runbook for FURY itself), `tests/loadtest.py` (about 440 req/s, p95 about 250 ms, 0 server errors against fake backends).
- Hardening: browser security headers, `fury/secrets.py` (AWS Secrets Manager, allow-listed keys, stub-tested only), `deploy/Caddyfile` and `deploy/Dockerfile` (not run), `pip-audit` clean on `requirements.txt`, waitress used automatically as the production server when installed.
- UI: rebuilt as a minimal single page (chat, incidents, insights), in-page sign-in instead of a browser prompt, light and dark themes, no external fonts or network calls.

## Added 2026-11-19 (second pass)
- **Chat now actually uses the model.** Root cause of the canned replies: the configured model (`openai/gpt-oss-20b`) was retired on 2026-11-18 and every call failed silently. Fixed with a model fallback chain (`nvidia/nemotron-3.5-bolt-e4b`, then two fallbacks) that skips dead models. Replies are phrased by the model from facts FURY's code supplies (runbook text, KB text, ticket and outcome facts), validated (no links, no credential words, no invented ticket numbers) and fall back to fixed text. Actions and approvals remain deterministic code; the model never decides to change anything. Verified live.
- **ServiceNow knowledge ingestion** (`fury/kb.py`): published articles are pulled, chunked, cited and used to answer how/why questions, always labelled informational and never given to the agents that act. Unit-tested; this instance has no published articles yet, so seed samples with `servicenow_setup/fury_snow_kb_seed.js` (not yet run).
- **Semantic (embedding) search** (`fury/vectors.py`, third ranker in `fury/retrieval.py`): built and unit-tested with fakes; embedding calls work live. **Off by default** (`FURY_SEMANTIC=on`) until the relevance floor is calibrated against the golden set, so it cannot weaken the abstain-without-evidence rule.
- **Work-notes access rule**: `servicenow_setup/fury_snow_acl.js` (admin runs once). Not yet applied.
- **Disaster recovery**: `fury/backup.py` backup/verify/restore with hash and audit-chain checks; round trip and tamper detection tested.
- **Security testing without an outside tester**: `tests/test_pentest.py` (authorization matrix over every route and role, auth and input attacks), bandit and pip-audit; one real bug found and fixed (non-object JSON body crashed `/api/chat`); outbound URLs restricted to https/loopback. See `docs/SECURITY_TESTING.md`. This is not an independent penetration test.
