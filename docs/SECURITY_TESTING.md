# Security testing (free, repeatable)

An **independent** penetration test is a person outside the team attacking the system and signing a report. That has not
happened and cannot be replaced by tooling. This is the free first pass that catches the common mistakes and keeps catching
them on every change. Run all of it with `python -m unittest discover -s tests` plus the two scanners below.

## What runs
| Check | How | Last result |
|---|---|---|
| Authorization matrix: every `/api` route x every role (none, viewer, operator, admin) must match the declared route map; a new route that is forgotten fails the build | `tests/test_pentest.py::AuthorizationMatrix` | pass |
| Auth attacks: malformed/oversized/SQL-looking tokens, token in query string or cookie, brute force throttling (429, stays blocked), tokens stored hashed only | `tests/test_pentest.py::AuthAttacks` | pass |
| Input attacks: garbage/huge/odd-typed bodies, hostile session ids, script in chat text, path traversal, stack-trace leakage, wrong HTTP methods, UI free of HTML sinks | `tests/test_pentest.py::InputAttacks` | pass (found and fixed one real bug: a JSON list body crashed `/api/chat` with a 500) |
| Outbound URL safety: only https, or http on loopback (blocks `file://`, `ftp://`, etc. from a bad config) | `tests/test_pentest.py::ConfigSafety`, `fury/netsafe.py` | pass |
| Static analysis (SAST) | `python -m bandit -r fury run_fury.py -x tests` | 4 low findings, all in the offline comparison harness `fury/baseline.py` (silent `except`), none on the product path |
| Dependency vulnerabilities | `python -m pip_audit -r requirements.txt` | none known at last run |
| Browser hardening | CSP, `X-Frame-Options: DENY`, `nosniff`, no-referrer, `no-store` on API | tested |
| LLM safety | prompt-injection text in a chat message is treated as data; model output is rejected if it contains links, credential words or an invented ticket number | `tests/test_llm_compose.py` |

## Not covered (be honest about these)
- No independent tester, no social engineering, no physical or cloud-account review.
- No dynamic scan with a full web scanner (OWASP ZAP needs Java or Docker; run its baseline scan against a test deployment: `docker run -t zaproxy/zap-stable zap-baseline.py -t http://host:8080`).
- The AWS IAM policies and ServiceNow roles were reviewed by reading, not attacked.
- The TLS proxy and container files in `deploy/` have not been run.

## What an independent test should be given
This document, the route map (`fury/auth.py: required_role`), the approval flow (`fury/approvals.py`, `fury/policy.py`), and a test
deployment with fake integrations (`python run_fury.py --demo`). Priority targets: the remediation approval path, token handling,
and the prompt-injection surface of the chat.
