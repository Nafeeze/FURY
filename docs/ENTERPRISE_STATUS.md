# FURY: Enterprise Readiness Status

Honest status against the Architecture and Engineering Blueprint v1.0. "Verified" means covered by automated tests that
ran green in this repo (`python -m unittest discover -s tests`) and, where noted, a real server run. **Live verification as of 2026-11-19:**
ServiceNow (create, work note, resolve, assign, recurrence handling), Slack (approval post, approver check, Socket Mode, approve-and-resolve) and
**AWS** (real Windows Server 2022 in ap-southeast-2: read-only checks and approved actions through Systems Manager, CloudWatch alarms, metrics
publisher, least-privilege split) were exercised against real accounts. Full end-to-end proof: IIS (W3SVC) stopped on the real server, FURY opened
INC0010014 owned by FURY, an SRE Admin approved in Slack, FURY restarted W3SVC through SSM, re-checked, and resolved the ticket with full notes.
Everything marked UNVERIFIED below was written and unit-tested but never run for real.

## Phase scorecard

| Phase | Blueprint goal | State | What exists (verified) | What is NOT done |
|---|---|---|---|---|
| 0 | Assessment and baseline | Done | `docs/PHASE_0_ASSESSMENT.md` | none |
| 1 | Voice foundation | **Deferred** | Chat channel hardened: read-only by default, one in-flight request per session, idle reset, session-to-trace correlation | **Voice itself.** Needs GPT-Live-1 access and a WebRTC session endpoint; decision taken to stay chat-only until then |
| 2 | Durable orchestration | Done for current scope | SQLite durable runs, per-workflow hop/time/agent budgets, concurrent workflows safe, **resume after a restart** (checkpoints after the ticket opens and after the fix runs; a resumed run reuses its ticket, never repeats an executed action, and escalates instead of guessing when a fix was in flight when the process died; a time window and a resume-count limit stop crash loops) | LangGraph (not adopted: the existing bus already does bounded, observable delegation; migration not justified yet), Postgres. Resume has not been exercised against the real server (unit and orchestrator tests only) |
| 3 | Specialized agents | Done for current scope | Six agents with distinct, enforced roles; versioned input contracts validated before any agent runs; **output contracts checked on every completed task** (a malformed result fails the hop); structured, timestamped evidence | Model-assisted reasoning agents (everything that acts is deterministic by design; the model only phrases replies) |
| 4 | Intelligence and RAG | Foundation only | Runbook versioning and lifecycle (active/draft/retired), citations with version, governed retrieval (BM25 + fuzzy, RRF, access and status filters before ranking, abstains without evidence, staleness flag), golden set (recall@3 1.0, MRR 1.0, 17 queries, 4 unsupported queries correctly empty) | Embeddings and pgvector, ServiceNow knowledge ingestion, freshness reconciliation jobs, a larger golden set from real incidents. The current golden set is small and author-written: treat it as a regression baseline, not proof of real-world accuracy |
| 5 | Enterprise integrations | ServiceNow, Slack and AWS **verified live** | Real ServiceNow, AWS (read/act IAM split, CloudFormation stack in `deploy/cloudformation/`) and Slack adapters; resilience wrapper (read retries, never retry writes, circuit breaker); `/api/health`; per-user **rate limiting** (chat 20/min, API 600/min, audited); Slack approval messages lose their buttons once decided and stale clicks are ignored | MCP (direct API clients used instead) |
| 6 | Governed autonomy | Done for current actions | Risk tiers with deny-by-default (`fury/policy.py`), human approval with signed expiring tokens bound to action/target/ticket, durable audit trail of every request and outcome, one-attempt-then-escalate that survives restarts | Approval via a UI/Slack interactive buttons with identity from the IdP; richer risk scoring |
| 7 | Operational excellence | Done for current scope | SLOs and `/api/obs/slo`, tamper-evident hash-chained audit log, retention pruning, health endpoint, traces/metrics/dashboard, **durable traces** (`FURY_TRACE_DB`), backup/verify/restore with tamper detection (round trip tested) | A load test against the live integrations; shipping the audit log to write-once storage |
| 8 | Enterprise hardening | Partly done | Token auth with three roles, deny-by-default route map, hashed tokens, throttling and rate limits, audited denials, refuses to start unauthenticated with real integrations, security headers, in-repo pen-test suite plus bandit and pip-audit, DR backup/restore tests | **SSO/OIDC** (needs an identity provider), an **independent** penetration test, and running the TLS proxy/container files (`deploy/Caddyfile`, `Dockerfile`, `docker-compose.yml`) and the Secrets Manager loader against real infrastructure (UNVERIFIED: no Docker here; the `.dockerignore` and `/data` permission defects found by review are fixed) |

## Test evidence
Run `python -m unittest discover -s tests`. Suites: flow, adapters, observability, chat policy, store, runbook provenance,
session/trace, budgets, policy and audit, contracts, retrieval (golden set), resilience, ops (audit/SLO/retention), auth.
Real-server checks performed: restart persistence of runs; authenticated access (401/200), incident run, SLO/health/audit
endpoints and audit-chain verification.

## What blocks "enterprise ready" today (in priority order)
1. **SSO** (and running the Secrets Manager loader live) instead of static tokens and `.env`: needs an identity provider.
2. **Independent security review / pen test** of the API and the remediation path.
3. **Real incident data** to grow the retrieval golden set and to calibrate SLO thresholds.
4. **Voice** (optional, deferred) once GPT-Live-1 access exists.
(Live verification of ServiceNow, Slack and AWS is no longer on this list: done 2026-11-19.)

## Known limitations worth stating plainly
- Budgets and limits are cooperative: checked between steps, they never kill a step that is mid-flight (deliberate: abandoning a half-done action is less safe).
- The audit log detects tampering; it does not prevent it. Ship it to write-once storage for real assurance.
- Without `FURY_TRACE_DB`, telemetry is in memory (newest 2000 spans) and clears on restart; counters and histograms are always in memory.
- An approval that was pending when FURY stopped cannot be honoured (its signed token lived in memory). The resumed run posts a fresh approval request instead, and the old message is retired.
- Resume needs `FURY_DB`. If the process dies while a fix is being sent to the server, FURY cannot know whether it ran, so it will not repeat it: it escalates for a human to check.

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

## Added 2026-11-19 (third pass: completing the blueprint)
- **Resume after restart** (`Ctx.resume_interrupted`, `Triage` checkpoints, `Store` in-flight marker). Tests: `tests/test_resume.py` (10), including a simulated crash and restart through the orchestrator.
- **Agent output contracts** (`contracts.validate_output`, enforced in `Bus`). Tests in `tests/test_contracts.py`.
- **Rate limiting** per identity (`fury/auth.py`), audited, tested in `tests/test_auth.py`.
- **Durable traces** (`telemetry.persist`, `FURY_TRACE_DB`), tested in `tests/test_trace_store.py`.
- **Slack approval hygiene**: decided requests lose their buttons and show who decided; stale clicks are ignored. `tests/test_slack_outcome.py`.
- **Container files reviewed**: `.dockerignore` rules so `.env` and databases are never baked into an image, `/data` made writable for the non-root user, `deploy/docker-compose.yml` added (FURY behind Caddy, FURY port not published). Not built or run (no Docker here).
- **Semantic search**: model fallback chain with per-model cache keys (vectors from different models are never mixed) and a calibrated relevance floor.
- **Live AWS**: verified end to end on a real server (see the top of this file).
