"""Rule-based claim extraction. Every claim must carry a verbatim quote found in its source (Pydantic-enforced
shape + a grounding check); claims that fail are dropped and counted. No LLM is used in this build."""
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, model_validator

FMT = "%Y-%m-%dT%H:%M:%SZ"
WEIGHT = {"debrief": 5, "commit": 4, "pr": 3, "issue": 2, "slack": 1}

Trust = Literal["SOURCE-BACKED", "STATED BY LEAVER", "INFERRED", "CONFLICT", "MISSING EVIDENCE"]
Bucket = Literal["decisions", "unfinished", "risks", "contacts", "gaps"]


class Claim(BaseModel):
    bucket: Bucket
    trust: Trust
    component: str = "platform"
    attribute: str = ""
    value: str = ""
    summary: str
    timestamp: Optional[str] = None
    source_key: Optional[str] = None
    quote: Optional[str] = None
    conflict_group: Optional[str] = None
    suggested: bool = False
    routed_to: Optional[str] = None

    @model_validator(mode="after")
    def need_evidence(self):
        if self.trust != "MISSING EVIDENCE" and not (self.source_key and self.quote and len(self.quote.strip()) >= 3):
            raise ValueError("claim without a source quote is rejected")
        return self


TECH = {
    "redis": ("Redis", "message broker"), "kafka": ("Kafka", "message broker"),
    "rabbitmq": ("RabbitMQ", "message broker"), "sqs": ("SQS", "message broker"),
    "postgres": ("Postgres", "database"), "postgresql": ("Postgres", "database"),
    "mysql": ("MySQL", "database"), "mongodb": ("MongoDB", "database"), "dynamodb": ("DynamoDB", "database"),
    "stripe": ("Stripe", "payment provider"), "razorpay": ("Razorpay", "payment provider"),
    "adyen": ("Adyen", "payment provider"),
}
TECH_RE = re.compile(r"\b(" + "|".join(TECH) + r")\b", re.I)
DROP_RE = re.compile(r"(?i)\b(?:instead of|rather than|away from|from|replac\w*|drop\w*|deprecat\w*|remov\w*)\s+\S+")
DECISION = re.compile(
    r"(?i)\b(decid\w*|agreed|switch\w*|migrat\w*|mov(?:e|ed|ing) to|go(?:ing)? with|let'?s use|"
    r"we(?:'ll| will) use|replac\w*|adopt\w*|chose|chosen|standardi[sz]\w*)\b")
BLOCKER = re.compile(
    r"(?i)\b(blocked|blocker|waiting (?:on|for)|flaky|workaround|hack|todo|fixme|known issue|"
    r"don'?t touch|do not touch|careful|still pending)\b")
CONTACT = re.compile(
    r"\b(?:[Aa]sk|[Pp]ing|[Cc]ontact|[Tt]alk to|[Rr]each out to|[Ee]mail)\s+@?([A-Z][a-zA-Z.\-]+)"
    r"(?:\s+(?:at|from|@)\s+([A-Z][\w&.\-]+))?")
NOT_NAMES = {"Me", "You", "Team", "Everyone", "Slack", "Jira", "Anyone", "Someone", "Them"}
COMP = [("webhook", "webhooks"), ("ledger", "ledger"), ("auth", "auth"), ("payout", "payouts"),
        ("reconcil", "reconciliation"), ("refund", "refunds"), ("settle", "settlement"),
        ("checkout", "checkout"), ("notification", "notifications")]

GAP_CHECKS = [
    ("deployment ownership", "Who owns production deployments, and how are they run?",
     ["deploy", "release", "rollout", "ci/cd", "pipeline"], "manager"),
    ("on-call", "Who is on-call / owns incident response for this system?",
     ["on-call", "oncall", "pagerduty", "incident", "runbook"], "manager"),
    ("rollback", "What is the rollback procedure when a change goes wrong?",
     ["rollback", "roll back", "revert"], "leaver"),
    ("monitoring", "Where are monitoring and alerting configured?",
     ["alert", "grafana", "datadog", "sentry", "prometheus", "monitor"], "leaver"),
    ("secret rotation", "How are production secrets rotated and who holds them?",
     ["rotate", "rotation", "vault", "secrets manager", "key management"], "leaver"),
]

FALLBACK_QUESTIONS = [
    ("Which external services, vendors or APIs did you have to babysit (rate limits, flaky sandboxes, manual steps)? "
     "What should your successor know?", "risks"),
    ("If something breaks on a weekend, what are the first three things you would check, and who would you call?", "risks"),
    ("What is the one piece of technical debt most likely to bite the next owner, and why was it never fixed?", "risks"),
]


def _n(t):
    return re.sub(r"\s+", " ", (t or "")).strip()


def sentences(text):
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if len(p.strip()) >= 8]


def component_of(*texts):
    for t in texts:
        tl = t.lower()
        for k, name in COMP:
            if re.search(r"\b" + k, tl):
                return name
    return "platform"


def _dt(s):
    try:
        return datetime.strptime(s, FMT)
    except Exception:
        return None


def days_between(a, b):
    da, db = _dt(a), _dt(b)
    return (db - da).days if da and db else 0


def rank(s):
    return (WEIGHT.get(s["type"], 0), s["ts"] or "")


def first_line(t):
    return t.strip().splitlines()[0].strip()


def extract(data, leaver):
    me = (leaver or "").lower()
    sources = {f"{s['type']}|{s['ref']}": s for s in data["sources"]}
    key_of = {id(s): f"{s['type']}|{s['ref']}" for s in data["sources"]}
    claims, dropped = [], 0

    def add(**kw):
        nonlocal dropped
        try:
            c = Claim(**kw)
        except ValidationError:
            dropped += 1
            return None
        if c.trust != "MISSING EVIDENCE":
            src = sources.get(c.source_key)
            if not src or _n(c.quote) not in _n(src["text"]):
                dropped += 1
                return None
        claims.append(c)
        return c

    # ---------- 1. decisions (with conflict detection) ----------
    cands, generic = {}, []
    for s in data["sources"]:
        if s["type"] == "debrief":
            continue
        for sent in sentences(s["text"]):
            if not DECISION.search(sent):
                continue
            cleaned = DROP_RE.sub(" ", sent)
            seen, found = set(), False
            for m in TECH_RE.finditer(cleaned):
                val, cat = TECH[m.group(1).lower()]
                if val == "Redis" and re.search(r"(?i)\b(cache|caching|session)", sent):
                    cat = "cache"
                if cat in seen:
                    continue
                seen.add(cat)
                found = True
                comp = component_of(sent, s["text"])
                k = (comp, cat, val)
                if k not in cands or rank(s) > rank(cands[k]["s"]):
                    cands[k] = {"component": comp, "attribute": cat, "value": val, "s": s, "sent": sent}
            if not found and re.search(r"(?i)\b(decid\w*|agreed)\b", sent):
                generic.append((s, sent))

    groups = defaultdict(list)
    for c in cands.values():
        groups[(c["component"], c["attribute"])].append(c)
    for (comp, attr), items in groups.items():
        conflict = len({i["value"] for i in items}) >= 2
        best = max(items, key=lambda i: rank(i["s"]))
        for i in sorted(items, key=lambda i: i["s"]["ts"]):
            add(bucket="decisions", trust="CONFLICT" if conflict else "SOURCE-BACKED", component=comp, attribute=attr,
                value=i["value"], summary=f"{i['value']} is the {attr} for {comp}", timestamp=i["s"]["ts"],
                source_key=key_of[id(i["s"])], quote=i["sent"], conflict_group=f"{comp}/{attr}" if conflict else None,
                suggested=conflict and i is best)

    seen_sent = set()
    for s, sent in sorted(generic, key=lambda x: x[0]["ts"], reverse=True)[:10]:
        if sent in seen_sent:
            continue
        seen_sent.add(sent)
        comp = component_of(sent, s["text"])
        add(bucket="decisions", trust="SOURCE-BACKED", component=comp, attribute="decision", value=sent[:140],
            summary=sent if len(sent) < 220 else sent[:217] + "...", timestamp=s["ts"],
            source_key=key_of[id(s)], quote=sent)

    # ---------- 2. unfinished work & blockers ----------
    commit_by_sha = {c["sha"][:7]: c for c in data["commits"]}
    as_of = data["as_of"]
    unmerged = [b for b in data["branches"] if not b["merged"] and b["name"] not in ("main", "master")]
    for b in sorted(unmerged, key=lambda b: b["last_commit_date"], reverse=True):
        c = commit_by_sha.get(b["last_commit_sha"][:7])
        if not c:
            cs = [x for x in data["commits"] if x["branch"] == b["name"]]
            c = max(cs, key=lambda x: x["date"]) if cs else None
        if not c:
            continue
        age = days_between(c["date"], as_of)
        ahead = f"{b['ahead_by']} commits ahead of main, " if b.get("ahead_by") else ""
        dormant = f" — dormant for {age} days" if age > 14 else ""
        add(bucket="unfinished", trust="SOURCE-BACKED", component=component_of(b["name"], c["message"]),
            attribute="unmerged branch", value=b["name"],
            summary=f'Branch "{b["name"]}" is unmerged ({ahead}last commit {c["date"][:10]} by {c["author"]}){dormant}.',
            timestamp=c["date"], source_key=f"commit|{c['sha'][:7]}", quote=first_line(c["message"]))

    for p in data["prs"]:
        if p["state"] == "open":
            add(bucket="unfinished", trust="SOURCE-BACKED", component=component_of(p["title"]),
                attribute="open PR", value=f"#{p['number']}", summary=f"PR #{p['number']} is still open: {p['title']}",
                timestamp=p["created_at"], source_key=f"pr|PR #{p['number']}", quote=p["title"])

    for s in data["sources"]:
        if s["type"] == "issue" and me and me in (s["meta"].get("assignee") or "").lower() \
                and (s["meta"].get("status") or "").lower() not in ("done", "closed", "resolved", "cancelled", "canceled"):
            add(bucket="unfinished", trust="SOURCE-BACKED", component=component_of(s["text"]), attribute="open ticket",
                value=s["ref"], summary=f"{s['ref']} is {s['meta'].get('status') or 'open'} and assigned to {leaver}: "
                f"{first_line(s['text']).split(': ', 1)[-1]}", timestamp=s["ts"],
                source_key=key_of[id(s)], quote=first_line(s["text"]))

    blockers, seen_b = [], set()
    for s in data["sources"]:
        if s["type"] == "debrief":
            continue
        for sent in sentences(s["text"]):
            if BLOCKER.search(sent) and sent not in seen_b:
                seen_b.add(sent)
                blockers.append((s, sent))
    for s, sent in sorted(blockers, key=lambda x: x[0]["ts"], reverse=True)[:8]:
        add(bucket="unfinished", trust="SOURCE-BACKED", component=component_of(sent, s["text"]), attribute="blocker",
            value=sent[:140], summary=f"Blocker / caveat: {sent}", timestamp=s["ts"],
            source_key=key_of[id(s)], quote=sent)

    # ---------- 3. bus-factor risk ----------
    by_file = defaultdict(list)
    for c in data["commits"]:
        for f in c["files"]:
            by_file[f].append(c)
    risky = [(f, cs) for f, cs in by_file.items()
             if len(cs) >= 2 and {x["author"].lower() for x in cs} == {me}]
    for f, cs in sorted(risky, key=lambda x: -len(x[1]))[:8]:
        last = max(cs, key=lambda x: x["date"])
        add(bucket="risks", trust="INFERRED", component=component_of(f), attribute="bus factor", value=f,
            summary=f"`{f}` has only ever been changed by {leaver} ({len(cs)} commits) — bus-factor risk.",
            timestamp=last["date"], source_key=f"commit|{last['sha'][:7]}", quote=first_line(last["message"]))

    # ---------- 4. contacts ----------
    pair, latest = Counter(), {}
    for p in data["prs"]:
        if p["author"].lower() != me:
            continue
        dirs = {(f.split("/")[0] + "/") if "/" in f else "(root)" for f in p["files"]}
        for r in p["reviewers"]:
            if r.lower() == me:
                continue
            for d in dirs:
                pair[(r, d)] += 1
                if (r, d) not in latest or p["created_at"] > latest[(r, d)]["created_at"]:
                    latest[(r, d)] = p
    for (r, d), n in pair.most_common(6):
        if n >= 2:
            p = latest[(r, d)]
            add(bucket="contacts", trust="INFERRED", component=component_of(d), attribute="reviewer", value=r,
                summary=f"{r} reviewed {n} of {leaver}'s PRs touching `{d}` — go-to person for that area.",
                timestamp=p["created_at"], source_key=f"pr|PR #{p['number']}", quote=p["title"])

    seen_c = set()
    for s in sorted(data["sources"], key=lambda x: x["ts"]):
        if s["type"] not in ("slack", "pr", "issue"):
            continue
        for sent in sentences(s["text"]):
            for m in CONTACT.finditer(sent):
                name, org = m.group(1).rstrip(".-"), m.group(2)
                if name in NOT_NAMES or name.lower() == me or name in seen_c:
                    continue
                seen_c.add(name)
                add(bucket="contacts", trust="SOURCE-BACKED", component=component_of(sent), attribute="contact",
                    value=name, summary=f"Contact: {name}{f' ({org})' if org else ''} — {sent}",
                    timestamp=s["ts"], source_key=key_of[id(s)], quote=sent)

    # ---------- 5. gap detection ----------
    corpus = "\n".join(s["text"].lower() for s in data["sources"])
    for topic, question, kws, route in GAP_CHECKS:
        if not any(k in corpus for k in kws):
            add(bucket="gaps", trust="MISSING EVIDENCE", component="platform", attribute=topic,
                summary=f"No source answers: {question}", routed_to=route)
    return claims, dropped


def debrief_questions(data, leaver):
    """3 targeted questions: dormant branches first, then other gaps via the fixed fallbacks."""
    as_of = data["as_of"]
    commit_by_sha = {c["sha"][:7]: c for c in data["commits"]}
    cands = []
    for b in data["branches"]:
        if b["merged"] or b["name"] in ("main", "master"):
            continue
        c = commit_by_sha.get(b["last_commit_sha"][:7])
        age = days_between(c["date"] if c else b["last_commit_date"], as_of)
        if age > 14:
            cands.append((b["ahead_by"] or 0, age, b))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    qs = []
    for ahead, age, b in cands[:3]:
        qs.append((f'Branch "{b["name"]}" has {ahead or "some"} unmerged commits and has been idle for {age} days. '
                   f"Why did it stop, what state is it in, and what should the successor do with it?", "unfinished"))
    for q in FALLBACK_QUESTIONS:
        if len(qs) >= 3:
            break
        qs.append(q)
    return qs[:3]
