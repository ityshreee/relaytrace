"""Parsers for offline exports: GitHub JSON, Slack JSON, Jira/Linear CSV.
Each returns normalised "sources" (the evidence units claims must quote) plus structured data."""
import csv
import io
import json
from datetime import datetime, timezone

FMT = "%Y-%m-%dT%H:%M:%SZ"


class IngestError(ValueError):
    pass


def to_iso(v):
    if v is None or v == "":
        return ""
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit() and len(v) >= 9):
            return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime(FMT)
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime(FMT)
    except Exception:
        return ""


def _name(x):
    if isinstance(x, dict):
        return str(x.get("login") or x.get("name") or x.get("username") or x.get("real_name") or "")
    return str(x or "")


def load_json(raw: bytes, label: str):
    if not raw or not raw.strip():
        raise IngestError(f"{label} file is empty")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise IngestError(f"{label} file is not valid JSON")


def read_jira_csv(raw: bytes):
    if not raw or not raw.strip():
        raise IngestError("Jira/Linear CSV is empty")
    try:
        text = raw.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception:
        raise IngestError("Jira/Linear CSV could not be read")
    if not rows:
        raise IngestError("Jira/Linear CSV has no rows")
    return rows


def parse_github(obj):
    if not isinstance(obj, dict) or not any(k in obj for k in ("commits", "pull_requests", "branches")):
        raise IngestError("GitHub JSON must be an object with 'commits', 'pull_requests' and/or 'branches'")
    sources, commits, prs, branches = [], [], [], []

    for c in obj.get("commits") or []:
        inner = c.get("commit") or {}
        sha = str(c.get("sha") or "")
        msg = c.get("message") or inner.get("message") or ""
        if not sha or not msg:
            continue
        author = _name(c.get("author")) or _name(inner.get("author"))
        date = to_iso(c.get("date") or (inner.get("author") or {}).get("date"))
        files = [f.get("filename") if isinstance(f, dict) else f for f in (c.get("files") or [])]
        rec = {"sha": sha, "author": author, "date": date, "message": msg, "files": [f for f in files if f],
               "branch": c.get("branch") or "main", "patch": c.get("patch") or ""}
        commits.append(rec)
        sources.append({"type": "commit", "ref": sha[:7], "ts": date, "author": author, "text": msg,
                        "meta": {"sha": sha, "files": rec["files"], "patch": rec["patch"], "branch": rec["branch"]}})

    for p in obj.get("pull_requests") or []:
        n = p.get("number")
        title = p.get("title") or ""
        if n is None or not title:
            continue
        author = _name(p.get("author") or p.get("user"))
        created = to_iso(p.get("created_at") or p.get("date"))
        body = p.get("body") or ""
        rev = [_name(r) for r in (p.get("reviewers") or [])]
        files = [f.get("filename") if isinstance(f, dict) else f for f in (p.get("files") or [])]
        rec = {"number": n, "title": title, "author": author, "state": str(p.get("state") or "").lower(),
               "merged": bool(p.get("merged")), "created_at": created, "reviewers": [r for r in rev if r],
               "files": [f for f in files if f], "branch": p.get("branch") or ""}
        prs.append(rec)
        sources.append({"type": "pr", "ref": f"PR #{n}", "ts": created, "author": author,
                        "text": f"{title}\n\n{body}".strip(),
                        "meta": {"number": n, "state": rec["state"], "merged": rec["merged"],
                                 "reviewers": rec["reviewers"], "files": rec["files"], "branch": rec["branch"]}})
        for k, cm in enumerate(p.get("comments") or [], 1):
            b = cm.get("body") or ""
            if not b.strip():
                continue
            sources.append({"type": "pr", "ref": f"PR #{n} comment {k}", "ts": to_iso(cm.get("created_at") or cm.get("date")),
                            "author": _name(cm.get("author") or cm.get("user")), "text": b,
                            "meta": {"number": n, "comment": True}})

    for b in obj.get("branches") or []:
        if b.get("name"):
            branches.append({"name": b["name"], "author": _name(b.get("author")), "merged": bool(b.get("merged")),
                             "ahead_by": b.get("ahead_by"), "last_commit_sha": str(b.get("last_commit_sha") or ""),
                             "last_commit_date": to_iso(b.get("last_commit_date"))})
    return {"sources": sources, "commits": commits, "prs": prs, "branches": branches}


def parse_slack(obj):
    if isinstance(obj, list):
        chans = {"#slack": obj}
    elif isinstance(obj, dict) and isinstance(obj.get("messages"), list):
        chans = {obj.get("channel") or "#slack": obj["messages"]}
    elif isinstance(obj, dict) and isinstance(obj.get("channels"), dict):
        chans = obj["channels"]
    else:
        raise IngestError("Slack JSON must be a message list, {'messages': [...]} or {'channels': {name: [...]}}")
    sources, seen = [], set()
    for chan, msgs in chans.items():
        clean = []
        for m in msgs or []:
            if not isinstance(m, dict) or m.get("subtype") in ("channel_join", "channel_leave"):
                continue
            text = (m.get("text") or "").strip()
            if not text:
                continue
            clean.append({"ts": to_iso(m.get("ts")), "author": _name(m.get("user_name") or m.get("user")
                          or (m.get("user_profile") or {}).get("real_name")), "text": text})
        clean.sort(key=lambda x: x["ts"])
        for i, m in enumerate(clean):
            ref = f"{chan} · {m['ts'][:16].replace('T', ' ')} · {m['author']}"
            while ref in seen:
                ref += "'"
            seen.add(ref)
            ctx = [{"author": x["author"], "ts": x["ts"], "text": x["text"]} for x in clean[max(0, i - 2):i + 3]]
            sources.append({"type": "slack", "ref": ref, "ts": m["ts"], "author": m["author"], "text": m["text"],
                            "meta": {"channel": chan, "context": ctx}})
    return sources


def parse_jira(rows):
    def pick(row, *names):
        low = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
        for n in names:
            if n in low and str(low[n]).strip():
                return str(low[n]).strip()
        return ""

    sources = []
    for row in rows:
        key = pick(row, "issue key", "key", "id")
        summary = pick(row, "summary", "title")
        if not key or not summary:
            continue
        desc = pick(row, "description")
        comments = pick(row, "comments", "comment")
        text = f"{key}: {summary}"
        if desc:
            text += f"\n{desc}"
        if comments:
            text += f"\nComments: {comments}"
        ts = to_iso(pick(row, "updated", "created"))
        sources.append({"type": "issue", "ref": key, "ts": ts, "author": pick(row, "reporter"), "text": text,
                        "meta": {"status": pick(row, "status"), "assignee": pick(row, "assignee"),
                                 "reporter": pick(row, "reporter"), "created": to_iso(pick(row, "created")),
                                 "updated": to_iso(pick(row, "updated"))}})
    if not sources:
        raise IngestError("Jira/Linear CSV needs at least 'key' and 'summary' columns")
    return sources
