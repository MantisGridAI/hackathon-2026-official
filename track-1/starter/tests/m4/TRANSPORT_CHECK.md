# Offline transport and prompt containment checks

The first live integration exposed a real defect: the provider request lasted
71.031 seconds despite a shorter configured timeout. The former HTTP client's
timeout applied to socket operations, rather than the total request wall time.
No additional provider calls were made while developing or checking this repair.

Production requests now run in one short-lived stdlib HTTP worker, with the
parent enforcing a monotonic absolute wall deadline. The Windows worker uses
the direct base interpreter, not a virtual-environment launcher. The parent
terminates and reaps the worker on expiry. No retry or background local request
survives that return. Closing a connection does **not** prove the provider stopped
computing or charging: missing usage remains unknown and the conservative cost
reservation remains charged to the internal budget.

Worker initialization, DNS, proxy/TLS setup and trickling response reads all fall
inside that deadline. Redirects are rejected, the endpoint/key come only from
the existing environment, response bodies are capped at 262144 bytes, and error
logs omit exception messages, credentials, response bodies and request headers.
401/403/5xx retain the existing sanitized error taxonomy. A startup failure before
the HTTP-attempt marker does not add an invented provider call to the ledger.

Executed on Windows with Python 3.12.14:

```
python -m unittest discover -s tests/m4 -p 'test_*.py' -v
# 48 passed; 1 real-telemetry test explicitly skipped without RCA_TEST_DATA.

<root-venv-python> -m unittest tests.integration.test_http_transport -v
# Both existing root integration tests passed against this worktree's modules.
```

The new loopback server tests send one response byte every 0.04 seconds. A 0.8-second
request budget still kills and reaps the worker, and the local server observes
the socket close. Separate tests cover a stalled worker startup, retained unknown
usage/reservations, the environment endpoint, blocked redirects, 401 and oversized
responses. These are synthetic local HTTP records, not real model evidence.

Prompts now have a global 48000-byte UTF-8 ceiling, including message framing.
They keep exact scalar excerpts, original candidate/evidence IDs, at least one
shown supporting record per offered candidate, and explicit omission metadata.
Original evidence ledgers are unchanged. Large synthetic input exercises the
global limit and reference integrity; the router also rejects oversized messages
before initializing a client or making any HTTP attempt.

An offline reconstruction used the two actual saved integration ledgers. Because
raw prompts and candidate feature vectors were deliberately not persisted, this
replays the recorded selected/alternative IDs and their support, **not** the full
original live prompts. Original missing reasons/features are explicitly marked
as unavailable. The following are measured serialization sizes, not token counts
or demonstrated live cost/accuracy improvements:

| Case key | Ledger records | Offered candidates before/after | Evidence excerpts before/after | Prompt bytes before/after |
|---|---:|---:|---:|---:|
| `925b67ad06d522ae` | 1720 | 12 / 12 | 7 / 2 | 27864 / 10327 |
| `d78cd53bdb510522` | 1673 | 12 / 12 | 56 / 5 | 189653 / 17418 |

Reproduce from the starter directory, supplying those saved evidence ledgers:

```
python tests/m4/replay_prompt_budget.py 7bc31e7 <ledger-1.json> <ledger-2.json>
```

Tradeoffs: each actual HTTP attempt incurs process startup and loses connection
pool reuse. Prompt excerpts omit correlated measurements and some alternatives;
that accuracy tradeoff has not been measured live. The existing allowed models,
output-token limit, ranking, tool budget and fallback policy remain in place.
