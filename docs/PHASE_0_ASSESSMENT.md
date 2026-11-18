# FURY — Phase 0 Repository Assessment and Baseline

Prepared against this repository and `FURY_Architecture_Engineering_Blueprint_v1_0.md`. Every claim below was verified by reading the source and, where noted, by actually running it — not inferred from file names.

**Baseline verified on 2026-11-18:** `python3 -m unittest discover -s tests` → **32/32 tests pass**. `python3 demo.py disk_full` reproduces the full alarm → runbook → approval → fix → verify → resolve flow exactly as documented below.

## 0. Executive summary

FURY today is a working, single-process text/Slack SRE-triage prototype (~2,500 lines of Python, six weeks of iteration, one git commit before this one, 32 passing unit tests). It already implements real versions of the blueprint's Agent Execution Plane (six agents), Enterprise Integration Plane (AWS/ServiceNow/Slack with fake/real toggles), approval governance, and a telemetry/observability dashboard. It has no voice channel, no durable state, no LangGraph orchestrator, no RAG, and no authentication/authorization. These are the real gaps — not the agents or integrations, which are solid.

## 1. Current-state architecture

**Location:** `fury/fury-core/` (this repo).
**Runtime:** Python, single process, Flask dev server, threaded.
**Entry point:** `run_fury.py` — real mode (AWS+ServiceNow+Slack) or `--demo` (all fakes, injectable faults, no external calls).
**Dependencies:** Flask 3.x, boto3, slack_bolt. OpenTelemetry is optional; telemetry falls back to an in-process recorder with the same call shape when it's absent.
**Deployment model:** one host, one Flask process, UI served from `ui/index.html`. No containerization, no process manager, no load balancer.

**Flow at a glance:** CloudWatch alarms (polled every 30s) and chat messages (HTTP) both reach `webapp.py`, which hands each to `Ctx.submit()` on a worker thread into the Agent Bus (`fury/a2a.py`), starting at **Triage**. Triage delegates by skill to **Knowledge** (runbook match), **Diagnostic** (read-only checks), **Remediation** (the only agent that can act, approval-gated), **ITSM** (ServiceNow), and **Comms** (Slack approval + escalation).

All state — `Ctx.runs`, `Triage.ticket`, `Remediation.done`, the pending-approvals dict, the LLM session store — lives in process memory and is lost on restart. There is no database.

**Trust boundary today:** the Flask app has no login. Anyone who can reach it can submit reports, read all incident/dashboard data, and (in demo mode) inject faults. The one real gate in the whole system: remediation requires a Slack approval from an allow-listed Slack user ID, enforced by a signed HMAC token (`fury/approvals.py`).

## 2. Capability inventory

- **Event ingestion** (`fury/aws.py` `AlarmPoller`): polls CloudWatch every 30s for `Fury-*` alarms, de-duped by name + state-change timestamp. Chat is the second front door.
- **Agent Bus** (`fury/a2a.py`): real skill-discovery dispatch, `Task` states (`submitted/working/input-required/completed/failed`), every hop recorded with latency. `RemoteAgent`/`serve()` exist for running an agent as its own HTTP process — built, unused today.
- **Agents** (`fury/agents.py`): Triage (owns the incident), Knowledge (keyword-matches 4 runbooks — not RAG), Diagnostic (read-only, hard tool allowlist), Remediation (only agent with `act()`, hard-coded action catalog, one-attempt-then-escalate), ITSM (ServiceNow open/note/assign/resolve), Comms (Slack approval + L2 escalation).
- **Integrations** (`fury/adapters.py`, `fury/aws.py`, `fury/slack_chat.py`): ServiceNow via stdlib `urllib` Table API client with Basic auth and auto-detected close codes. AWS via **two separate IAM sessions** — one can only run read SSM documents, one only act documents — a real least-privilege split, tested (`check_all.py`) to prove the read session is denied an action. Slack via `slack_bolt` Socket Mode (outbound only, no public URL). Every integration has a same-interface fake used by `--demo`, tests, and `compare.py`.
- **Approvals/governance** (`fury/approvals.py`): signed, expiring (10 min default), single-purpose HMAC tokens. Comms signs only after a real approver replies; Remediation independently re-verifies before acting.
- **Knowledge** (`fury/runbooks.py`, `runbooks/*.md`): 4 markdown runbooks, keyword/token-overlap matched. No embeddings, no citations beyond runbook id/title, no versioning/freshness metadata.
- **Telemetry** (`fury/telemetry.py`, 429 lines — the largest file): span/counter/histogram API, consistent whether or not real OpenTelemetry is installed. Redaction happens at capture time. Retention: newest 2000 spans, cleared on restart — no persistence.
- **LLM layer, optional** (`fury/llm.py`): classifies chat intent only; architecturally cannot trigger an action (no reference to the Bus/snow/chat/target). Off by default; falls back to deterministic rules on any failure.
- **UI** (`ui/index.html`): Chat / Incidents / Observability tabs. Session id is a random per-tab UUID — a conversation key, not an identity.
- **Dev/ops tooling**: `demo.py` (scripted run + full trace print), `check_all.py` (live-credential smoke test, never prints secrets), `compare.py` (multi-agent vs. single-agent baseline comparison across 9 scenarios — correctness, ticket completeness, hop count, *unsafe actions executed*), `aws_setup/*` (provisions SSM documents + CloudWatch alarms).

## 3. Workflow map (verified by running `demo.py disk_full`)

1. **Knowledge** matches a runbook by keyword score.
2. **Diagnostic** runs the runbook's leading checks (or generic checks with no runbook). *Failure: false alarm* if the first check reports healthy — stop, no ticket.
3. **ITSM** opens an incident, deduping by key. *Failure: duplicate* if one's already open. *Failure: ServiceNow unreachable* → escalate immediately, no action without a ticket.
4. Evidence note added. *Failure: no runbook* → escalate.
5. Remaining runbook steps: `decide` (hard-coded rule per runbook id) — *failure: decision says don't proceed* → escalate; `action` → Remediation checks catalog + one-attempt limit, Comms requests Slack approval and blocks until an allow-listed approver replies or TTL expires, Remediation independently re-verifies the token — *failures: rejected / timeout / unauthorized reply (explicitly ignored, not rejected) / second attempt* → escalate; `check` after action re-verifies — *failure: still unhealthy* → escalate "verification failed after the fix"; else resolve.
6. **Escalate** (shared path): assign to the runbook's team (or default L2), post escalation message.

Every path has a dedicated test in `tests/test_flow.py`; most are also exercised in `compare.py`.

## 4. Security assessment

**Solid:** AWS least-privilege split (tested), two-point approval verification, unauthorized Slack repliers explicitly ignored, secrets env-var based and never logged, `.env*` gitignored, telemetry redaction at capture time (unit-tested), LLM classifier structurally unable to act.

**Missing, in priority order:**
1. No authentication/authorization on the web UI or API — anyone reachable can submit reports, read everything, (in demo mode) inject faults.
2. No RBAC — the only access-control concept anywhere is the Slack approver allow-list for one action.
3. No secrets manager — `.env` file, fine for a prototype, not enterprise.
4. Demo-mode fault-injection routes are only kept off a real target by how `run_fury.py` computes `demo=` — operational discipline, not a hard control.
5. No audit trail of *who* opened a chat session beyond the one approver identity on the one gated action.

## 5. Gap analysis vs. target architecture

| Plane / Phase | Target | Current | Gap |
|---|---|---|---|
| Conversational Experience / Ph.1 | GPT-Live-1, WebRTC, full-duplex | Text chat + Slack only | Complete — no voice at all |
| Control Plane / Ph.2 | LangGraph-justified, durable, bounded delegation | Real skill-discovery Bus, bounded/observable — but in-memory, no checkpointing | Durability is the gap, not the dispatch model |
| Agent Execution / Ph.3 | 6 named agents, structured evidence/citations, budgets | Close 1:1 match; deterministic-first already the philosophy; evidence not yet structured objects; no explicit per-agent budgets | Needs schema work, not a rewrite |
| Model Gateway & Persistence / Ph.2&4 | Provider-abstracted gateway, Postgres, pgvector | One direct provider call for one narrow task; zero persistence | Large gap |
| Enterprise Integration / Ph.5 | MCP-preferred, rate limits, idempotency, audit | Direct API/SDK calls, narrow idempotency, no MCP | Partial — safe but not MCP-based |
| Knowledge & RAG / Ph.4 | Embeddings, mandatory metadata, citations, Recall@K eval | Keyword match, no embeddings/metadata | Large gap, but typed-step runbook format is a clean migration base |
| Security/Governance / Ph.6&8 | SSO, RBAC, autonomy tiers, pentest, secrets manager | None of the identity/hardening pieces exist; approval-gated-by-default philosophy already matches the conservative intent | Large gap on hardening specifically |
| Observability / Ph.7 | Tracing, metrics, data-handling policy | Already real: spans, redaction-at-capture, dashboard, eval harness | Smallest gap — mainly durable trace storage |

## 6. Migration plan

- **Retain as-is:** Bus/Agent/Task shape, the six agents' responsibilities, `approvals.py`, AWS read/act session split, fake adapters, `demo.py`/`compare.py`/`check_all.py`. Already match the blueprint's own "don't replace a working component to conform to a framework" instruction.
- **Refactor, don't replace:** in-memory state (`Ctx.runs`, `Remediation.done`, pending approvals) → swap the storage behind the same call sites (dict → SQLite now → Postgres later).
- **Refactor:** `fury/runbooks.py` — add `source_id`/`version`/`indexed_at` to front-matter and the `Runbook` object. Additive, not a rewrite.
- **Add, new surface:** voice front door calling the existing, unmodified `handle_report`.
- **Add, new surface:** durable store, ahead of voice (per blueprint §4.2 — a disconnect must not lose operational state).
- **Add, later phase:** LangGraph, pgvector RAG, MCP, SSO/RBAC, secrets manager — each needs calendar time this plan doesn't assume away.
- **Deprecate:** nothing. No working capability removed without an approved replacement already in place.

## 7. Test baseline (re-run, not just counted)

```
$ python3 -m unittest discover -s tests
Ran 32 tests in 1.970s
OK
```
- `tests/test_flow.py` — 19 tests (every outcome in §3).
- `tests/test_adapters.py` — 8 tests (SSM docs, alarm dedup, Slack parsing/auth).
- `tests/test_observability.py` — 5 tests (trace connectivity, PII-free traces, dashboard shape).
- `tests/eval_set.py` + `compare.py` — evaluation harnesses (behavioral scenarios, classifier accuracy, privacy check, multi- vs single-agent comparison), not unit tests.
- `check_all.py` — live-credential smoke test, not run here (needs real ServiceNow/AWS/Slack creds).

**Gap:** no HTTP-layer tests against `webapp.py`'s own Flask routes (flow tests exercise the Bus/agents directly). Nothing tests the live LLM call path directly — lower risk, since any failure there degrades to rules by design.

## 8. Prioritized backlog — Phase 1 (Voice Foundation), two-person split

No GPT-Live-1 access is currently provisioned. Scoped so Epic A ships a working voice experience today (browser Speech-to-Text/Text-to-Speech → existing, unmodified `handle_report`) and upgrades to full-duplex GPT-Live-1 later without a rewrite.

**Epic A — Voice front door (Person A):** push-to-talk tab in `ui/index.html`; `/api/voice/capabilities` + `/api/voice/session` (ephemeral-key brokering, never a standard key to the browser, 503+fallback if unconfigured); rate limit + input-length cap on voice-originated requests.
**Epic B — Durable state (Person B):** SQLite-backed store behind the existing call sites for runs/approvals/one-attempt tracking; `source_id`/`version`/`indexed_at` on runbooks, surfaced as evidence citations; correlate voice session id with the existing trace id.
**Joint (30–45 min):** wire the voice transcript into `handle_report`; confirm dashboard correlation.
**Explicitly deferred, say so in the demo:** LangGraph, pgvector RAG, MCP, SSO/RBAC, secrets manager, pentest.
**Decisions confirmed:** ServiceNow runs `fake` as a documented fallback while a real instance is provisioned; AWS uses the existing demo server; Slack stays real.
