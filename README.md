# RelayTrace

Evidence-linked project knowledge handover. Team TwinTech, CodeFrontier Round 2.

Upload GitHub / Slack / Jira exports. RelayTrace masks secrets, reconstructs decisions, unfinished work, bus-factor risks and contacts, asks the leaver 3 targeted questions, flags conflicts and missing evidence, and releases a role-filtered dossier to the successor. Every claim links to a verbatim quote in the original source.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000
```

A sample project (PayFlow-Core) is created automatically on first start. Use **New demo project** to create another, or **Upload exports** (sample files are downloadable from the upload dialog).

## Try the workflow (about 2 minutes)

1. Open the app as **Outgoing contributor**. Read the Day-1 roadmap and click any source link to open the evidence inspector (quote highlighted).
2. Resolve the amber **Redis vs Kafka** conflict.
3. Answer the 3-question debrief, then use **Answer this** on a *Missing evidence* card.
4. Reject / edit / hide claims from the successor (**Visible to successor** toggle).
5. **Approve all remaining**, then **Lock & release**.
6. Switch to **Successor** to see only approved, shared claims. Switch to **Manager** to see everything plus the redaction report.

## Architecture

| Layer | Implementation |
|---|---|
| API | FastAPI + Pydantic v2, SQLite (`app/db.py`) |
| Ingest | `app/parsers.py` (GitHub JSON, Slack JSON, Jira/Linear CSV) |
| Redaction | `app/scrubber.py` runs on every string *before* parsing or storage; the secret value is never persisted |
| Extraction | `app/extract.py`, rule-based; claims must pass the Pydantic `Claim` model and a grounding check (quote must appear verbatim in the cited source) or they are dropped |
| Conflicts | same `(component, attribute)` with different values; source weighting commit > PR > issue > chat picks the *suggested* value, the human decides |
| Gaps | keyword checks for deployment owner, on-call, rollback, monitoring, secret rotation produce `[MISSING EVIDENCE]` routed to leaver/manager |
| UI | single page, Tailwind CDN, vanilla JS (`app/static/index.html`) |

Trust taxonomy: `SOURCE-BACKED`, `STATED BY LEAVER`, `INFERRED` (aggregates such as bus factor and reviewer stats), `CONFLICT`, `MISSING EVIDENCE`.

## Known limitations (Round 2)

- Roles are chosen in a dropdown and sent as an `X-Role` header. The server enforces what each role can see and do, but there is no real authentication yet.
- Extraction is heuristic: decisions need a cue word plus a known technology; conflict detection depends on consistent component naming. English only.
- No LLM layer in this build.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
