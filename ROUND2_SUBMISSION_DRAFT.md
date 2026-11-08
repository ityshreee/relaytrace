# Round 2 submission draft (edit the bracketed parts)

## Working project link
[your deployed URL]

## Source-code repository link
[your public GitHub URL]

## Completed features
- Offline ingestion of GitHub JSON (commits, PRs, comments, branches), Slack JSON and Jira/Linear CSV, with clear errors for empty or corrupt files
- Pre-storage secret redaction (AWS, GitHub, Slack, Stripe, Google keys, JWTs, bearer tokens, private keys, URL credentials, generic `key=value` secrets) with a per-project redaction report that never stores the secret
- Rule-based reconstruction into Key Decisions, Unfinished Work & Blockers, Bus-Factor Risk and Contextual Contacts
- Claim schema enforced with Pydantic v2; claims without a verbatim source quote are rejected
- 5-tier trust taxonomy on every claim
- Conflict detection with source-weighting suggestion and one-click resolution (pick the current value or mark unknown)
- Missing-evidence detection for deployment ownership, on-call, rollback, monitoring and secret rotation, routed to leaver/manager
- 3-question leaver debrief generated from dormant branches, with fixed fallbacks; answers become [STATED BY LEAVER] claims
- Split-screen evidence inspector (commit diff, PR thread, ticket, Slack context) with the quoted passage highlighted
- Per-claim approve / reject / edit / undo and per-claim successor visibility
- Day-1 continuation roadmap
- Three server-enforced roles (leaver, manager, successor); successor access is blocked until the leaver locks the dossier and only sees approved, shared claims
- Lock & release workflow, blocked while conflicts remain open
- Synthetic PayFlow-Core dataset, downloadable sample files, automated end-to-end tests

## Technology and implementation details
Python 3.11+, FastAPI, Pydantic v2, SQLite; Tailwind CSS (CDN) and vanilla JavaScript single-page UI; deterministic regex/heuristic extraction with a grounding check; deployed on [Render / Hugging Face Spaces].

## Changes from the original plan
- React + Vite replaced by a single served HTML page to ship one deployable service.
- LangChain / LLM extraction replaced by rule-based extraction so judging never depends on API keys or rate limits. It is deterministic and every claim is quote-grounded.
- Roles are selected in the UI and enforced by the server via a request header; real authentication is deferred.
- Manager dual sign-off and successor receipt moved to Round 3 as planned.

## Three major improvements for Round 3
1. Temporal Conflict Radar with side-by-side timeline resolution across many components, plus a Gap Tracker that follows each missing-evidence item to resolution.
2. Contractor role tier and a version-history audit trail of every edit, rejection, approval and visibility change.
3. Successor feedback loop and formal digital acknowledgment of receipt with manager co-sign.
