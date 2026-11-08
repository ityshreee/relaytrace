import json
from collections import Counter
from datetime import datetime, timezone

from .extract import debrief_questions, extract
from .parsers import IngestError, parse_github, parse_jira, parse_slack
from .scrubber import scrub_obj


def build_project(conn, name, leaver, gh=None, slack=None, jira_rows=None):
    """Scrub -> parse -> extract -> persist. Returns project id. Raw (unscrubbed) data never touches the DB."""
    report = []
    sources, commits, prs, branches = [], [], [], []
    if gh is not None:
        g = parse_github(scrub_obj(gh, "github", report))
        sources += g["sources"]; commits += g["commits"]; prs += g["prs"]; branches += g["branches"]
    if slack is not None:
        sources += parse_slack(scrub_obj(slack, "slack", report))
    if jira_rows is not None:
        sources += parse_jira(scrub_obj(jira_rows, "jira", report))
    if not sources:
        raise IngestError("Upload at least one export (GitHub JSON, Slack JSON or Jira/Linear CSV)")

    as_of = max([s["ts"] for s in sources if s["ts"]] or [""])
    if not leaver:
        authors = Counter([c["author"] for c in commits] + [p["author"] for p in prs])
        leaver = authors.most_common(1)[0][0] if authors else "leaver"
    data = {"sources": sources, "commits": commits, "prs": prs, "branches": branches, "as_of": as_of}
    claims, dropped = extract(data, leaver)
    questions = debrief_questions(data, leaver)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute("INSERT INTO projects(name, leaver, as_of, created_at, dropped) VALUES(?,?,?,?,?)",
                       (name or "Untitled project", leaver, as_of, now, dropped))
    pid = cur.lastrowid
    ids = {}
    for s in sources:
        c = conn.execute("INSERT INTO sources(project_id,type,ref,ts,author,text,meta) VALUES(?,?,?,?,?,?,?)",
                         (pid, s["type"], s["ref"], s["ts"], s["author"], s["text"], json.dumps(s["meta"])))
        ids[f"{s['type']}|{s['ref']}"] = c.lastrowid
    for c in claims:
        text = c.summary
        conn.execute(
            "INSERT INTO claims(project_id,bucket,trust,component,attribute,value,summary,ts,source_id,quote,"
            "conflict_group,suggested,routed_to) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, c.bucket, c.trust, c.component, c.attribute, c.value, text, c.timestamp,
             ids.get(c.source_key), c.quote, c.conflict_group, int(c.suggested), c.routed_to))
    for i, (q, bucket) in enumerate(questions, 1):
        conn.execute("INSERT INTO debrief(project_id,idx,question,bucket,answer) VALUES(?,?,?,?,'')", (pid, i, q, bucket))
    for kind, loc in report:
        conn.execute("INSERT INTO redactions(project_id,kind,location) VALUES(?,?,?)", (pid, kind, loc))
    return pid
