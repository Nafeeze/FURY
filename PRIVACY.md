# Privacy handling (Week 6)

What FURY collects, where it goes, how long it lives, and what's deliberately
kept out of it. This is a description of the current implementation, not a
legal policy.

## What touches the LLM (NVIDIA API)

`fury/llm.py`'s `classify()` is the only code in the project that sends text
to an external model. It sends:
- the current chat message (capped at 2000 chars)
- a short reference block: up to 6 prior turns, each a one-line
  **already-classified summary** (e.g. `report (INC0010020): disk F is full`),
  never the original raw message text of earlier turns

It never sends: tool output, ServiceNow ticket contents, server check
results, approver identities, secrets, or anything from `webapp.py`'s
deterministic path. This is enforced by construction, not by convention:
`llm.py` has no reference to the Bus, `snow`, `chat`, or target adapters, so
it has nothing else to send even if the prompt were compromised.

## Session memory (`SessionStore`)

- In-memory only, never written to disk.
- Bounded to the last 6 turns per session, each turn truncated to 160 chars.
- Wiped outright whenever the model reports a subject change, so stale
  context about an old incident doesn't bleed into a new one.
- **New in Week 6:** every session also tracks when it was last touched.
  `purge_expired()` drops any session idle for more than `FURY_SESSION_TTL`
  seconds (default 3600) so a tab left open overnight doesn't keep that
  conversation's context alive indefinitely. This runs as a cheap sweep on
  every live `classify()` call, not on a background timer.
- The explicit `/api/chat/reset` endpoint still exists for an immediate wipe.

## Redaction (`fury/telemetry.py: redact()`)

Everything that goes into a trace span, a counter/histogram label, or the
eval report is passed through `redact()` first:
- emails, IPv4 addresses, AWS access key IDs, long digit runs
  (phone/card-like numbers), and `key=value` secret-looking pairs
  (`api_key=`, `password=`, `token=`, etc.) are masked before anything is
  recorded.
- This is a safety net, not the primary control -- the primary control is
  that `agents.py` and `webapp.py` never *construct* telemetry attributes out
  of full incident descriptions or ticket notes in the first place; only
  lengths, booleans, and enum-like values (skill name, outcome, intent) are
  normally attached.
- `tests/eval_set.py`'s privacy suite plants fake secrets and asserts they
  never survive into a `telemetry.summary()` dump, as a regression check on
  this guarantee.
- `FURY_TELEMETRY_RAW=on` disables redaction. It exists for local debugging
  only and must never be set against a real server or real ServiceNow data.

## Secrets (API keys, tokens)

- Read from environment variables only (`NVIDIA_API_KEY`, ServiceNow
  creds, Slack tokens), never hard-coded, never logged.
- `.env` is the one place they live on disk; `SETUP.md` already says not to
  paste it into chat. Nothing in this Week 6 change reads or writes `.env`.
- Not yet done (parked for Week 7 per the security/RBAC pass): secrets
  manager instead of a plain `.env` file.

## What's NOT private data here, by design of the system

FURY operates one Windows server's infrastructure metrics (disk/CPU/
memory/service state) and ServiceNow incident text about that server. There
is no end-user PII flowing through the system under normal operation. The
redaction rules above exist mainly for: (a) defense in depth if an engineer
pastes something sensitive into a chat message by mistake, and (b) keeping
the eval/telemetry reports themselves safe to share without a second review.

## How to check this yourself

```
python tests/eval_set.py          # includes a "Privacy eval" section
```

## Update 2026-11-19: what else the model now sees

`classify()` is no longer the only call. The chat now also asks the model to **phrase replies** from facts FURY's own code
supplies (`llm.compose`), and, if `FURY_SEMANTIC=on`, to **embed** text for semantic search (`llm.embed`).

Sent to the model for replies: the user's current message (capped), the static product description, and FACTS limited to
runbook text (authored by the team), ServiceNow **knowledge article** text (published, organisation-approved), ticket numbers,
states, team names, and outcome words (`resolved`, `escalated`, ...). Instance IDs and similar identifiers are scrubbed by
`telemetry.redact` first.

Still never sent: work notes, server check results/evidence, approver identities, secrets, or raw chat history.
Sent for embeddings: runbook and knowledge-article chunks (once, then cached) and the user's search question.

Model output is validated before it is shown: no links, no credential words, no ticket numbers that were not in the facts,
length-limited. On any failure FURY shows its fixed deterministic text instead.
