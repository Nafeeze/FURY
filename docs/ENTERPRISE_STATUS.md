# FURY: Enterprise Readiness Status

Honest status against the Architecture and Engineering Blueprint v1.0. "Verified" means covered by automated tests that
ran green in this repo (`python -m unittest discover -s tests`) and, where noted, a real server run. **Nothing in this
file has been verified against live AWS, ServiceNow or Slack**: those accounts did not exist at the time of writing.

## Phase scorecard

| Phase | Blueprint goal | State | What exists (verified) | What is NOT done |
|---|---|---|---|---|
| 0 | Assessment and baseline | Done | `docs/PHASE_0_ASSESSMENT.md` | none |
| 1 | Voice foundation | **Deferred** | Chat channel hardened: read-only by default, one in-flight request per session, idle reset, session-to-trace correlation | **Voice itself.** Needs GPT-Live-1 access and a WebRTC session endpoint; decision taken to stay chat-only until then |
| 2 | Durable orchestration | Mostly done | SQLite durable runs, restart recovery (interrupted runs marked failed, pending approvals expired), per-workflow hop/time/agent budgets, concurrent workflows safe | Workflow **resume** after a restart (interrupted work is failed and escalated, not continued), LangGraph (not adopted: the existing bus already does bounded, observable delegation; migration not justified yet), Postgres |
| 3 | Specialized agents | Done for current scope | Six agents with distinct, enforced roles; versioned input contracts validated before any agent runs; structured, timestamped evidence | Output schemas; model-assisted reasoning agents (everything is deterministic by design today) |
| 4 | Intelligence and RAG | Foundation only | Runbook versioning and lifecycle (active/draft/retired), citations with version, governed retrieval (BM25 + fuzzy, RRF, access and status filters before ranking, abstains without evidence, staleness flag), golden set (recall@3 1.0, MRR 1.0, 17 queries, 4 unsupported queries correctly empty) | Embeddings and pgvector, ServiceNow knowledge ingestion, freshness reconciliation jobs, a larger golden set from real incidents. The current golden set is small and author-written: treat it as a regression baseline, not proof of real-world accuracy |
| 5 | Enterprise integrations | Built, **unverified live** | Real ServiceNow, AWS (read/act IAM split) and Slack adapters; resilience wrapper (read retries, never retry writes, circuit breaker); `/api/health`; AWS/ServiceNow setup guide and scripts (branch `feature/aws-env-setup`) | **Any live run**; MCP (direct API clients used instead); rate limiting |
| 6 | Governed autonomy | Done for current actions | Risk tiers with deny-by-default (`fury/policy.py`), human approval with signed expiring tokens bound to action/target/ticket, durable audit trail of every request and outcome, one-attempt-then-escalate that survives restarts | Approval via a UI/Slack interactive buttons with identity from the IdP; richer risk scoring |
| 7 | Operational excellence | Core done | SLOs and `/api/obs/slo`, tamper-evident hash-chained audit log, retention pruning, health endpoint, traces/metrics/dashboard | Alerting on SLO breach, durable trace storage, runbooks for operating FURY itself, load testing |
| 8 | Enterprise hardening | Partly done | Token auth with three roles, deny-by-default route map, hashed tokens, throttling, audited denials, refuses to start unauthenticated with real integrations | **SSO/OIDC**, a secrets manager (secrets are still in `.env`), TLS (serve behind a reverse proxy), penetration test, dependency/supply-chain scanning, disaster recovery test |

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
