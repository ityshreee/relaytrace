import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import os

from .db import get_conn, init_db
from .demo_data import PROJECT_NAME, build_demo, demo_files
from .parsers import IngestError, load_json, read_jira_csv
from .pipeline import build_project

ROLES = ("leaver", "manager", "successor", "contractor")
RES = ("successor", "contractor")  # receiving roles: only ever see the released, approved, shared claims
# Demo-grade sign-in: one PIN per role, overridable with environment variables on a real deployment.
PINS = {r: os.environ.get(f"RELAYTRACE_PIN_{r.upper()}", d)
        for r, d in (("leaver", "2468"), ("manager", "1357"), ("successor", "9753"), ("contractor", "8642"))}
SECRET = os.environ.get("RELAYTRACE_SECRET", "relaytrace-demo-secret")
BUCKETS = ["decisions", "unfinished", "risks", "contacts", "gaps"]
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@asynccontextmanager
async def lifespan(app):
    init_db()
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            g, s, j = build_demo()
            from .parsers import read_jira_csv as rj
            pid = build_project(c, PROJECT_NAME, "arjun", g, s, rj(j.encode()))
            log_event(c, pid, "system", "project_created", summary="Sample project created on first start")
    yield


app = FastAPI(title="RelayTrace", lifespan=lifespan)


def make_token(role):
    sig = hmac.new(SECRET.encode(), f"{role}:{PINS[role]}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{role}.{sig}"


def get_role(x_token: str = Header(default="")):
    role = (x_token or "").split(".", 1)[0]
    if role not in ROLES or not hmac.compare_digest(x_token, make_token(role)):
        raise HTTPException(401, "Please sign in with your role PIN")
    return role


def log_event(c, pid, actor, action, claim=None, summary="", old=None, new=None):
    """Append-only audit trail: who did what, when, and what changed."""
    c.execute("INSERT INTO events(project_id,ts,actor,action,claim_id,summary,old,new) VALUES(?,?,?,?,?,?,?,?)",
              (pid, now(), actor, action, claim["id"] if claim else None, summary,
               None if old is None else str(old), None if new is None else str(new)))


def claim_text(cl, n=110):
    t = (cl["edited_text"] or cl["summary"] or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


class LoginIn(BaseModel):
    role: str
    pin: str


@app.post("/api/login")
def login(body: LoginIn):
    if body.role not in ROLES or not hmac.compare_digest(body.pin.strip(), PINS[body.role]):
        raise HTTPException(401, "Wrong PIN for that role")
    return {"token": make_token(body.role), "role": body.role}


def project_or_404(c, pid):
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Project not found")
    return p


def visible(row, role, scope=None):
    if role in ("leaver", "manager"):
        return True
    if not (row["status"] == "approved" and row["visibility"] == "all"):
        return False
    if role == "contractor":
        return row["component"] in (scope or ())
    return True


def scope_set(c, pid):
    return {r["component"] for r in c.execute("SELECT component FROM scope WHERE project_id=?", (pid,))}


def gap_of(r):
    """Gap-tracker state of a claim: open -> assigned -> answered -> verified (or dismissed). None if not a gap."""
    st = r["gap_state"]
    if not st and r["trust"] == "MISSING EVIDENCE":
        st = "open"
    if not st:
        return None
    if r["status"] == "rejected" and st != "verified":
        st = "dismissed"
    return {"state": st, "owner": r["gap_owner"] or "", "due": r["gap_due"] or ""}


def claim_view(r, role):
    d = {
        "id": r["id"], "bucket": r["bucket"], "trust": r["trust"], "component": r["component"],
        "attribute": r["attribute"], "value": r["value"], "text": r["edited_text"] or r["summary"],
        "edited": bool(r["edited_text"]), "original": r["summary"] if r["edited_text"] else None,
        "timestamp": r["ts"], "status": r["status"], "visibility": r["visibility"],
        "conflict_group": r["conflict_group"], "suggested": bool(r["suggested"]),
        "quote": r["quote"],
        "source": {"id": r["source_id"], "type": r["s_type"], "ref": r["s_ref"]} if r["source_id"] else None,
    }
    if role not in RES:
        d["routed_to"] = r["routed_to"]
        d["gap"] = gap_of(r)
    return d


def load_claim(c, cid):
    r = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Claim not found")
    return r


def leaver_edit_guard(c, claim, role):
    if role != "leaver":
        raise HTTPException(403, "Only the outgoing contributor can change claims")
    p = project_or_404(c, claim["project_id"])
    if p["status"] == "locked":
        raise HTTPException(409, "The dossier is locked")
    return p


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/projects")
def projects(role: str = Depends(get_role)):
    with get_conn() as c:
        rows = c.execute("SELECT id,name,leaver,status,created_at FROM projects ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def creators_only(role):
    if role in RES:
        raise HTTPException(403, "Only the outgoing contributor and managers can create projects")


@app.post("/api/demo")
def create_demo(role: str = Depends(get_role)):
    creators_only(role)
    g, s, j = build_demo()
    with get_conn() as c:
        pid = build_project(c, PROJECT_NAME, "arjun", g, s, read_jira_csv(j.encode()))
        log_event(c, pid, role, "project_created", summary="Sample project created")
    return {"id": pid}


@app.get("/api/demo-files/{name}")
def demo_file(name: str):
    files = demo_files()
    if name not in files:
        raise HTTPException(404, "No such sample file")
    mt = "text/csv" if name.endswith(".csv") else "application/json"
    return Response(files[name], media_type=mt, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/ingest")
async def ingest(name: str = Form("My project"), leaver: str = Form(""),
                 github: Optional[UploadFile] = File(None), slack: Optional[UploadFile] = File(None),
                 jira: Optional[UploadFile] = File(None), role: str = Depends(get_role)):
    creators_only(role)
    try:
        gh = sl = jr = None
        if github is not None and github.filename:
            gh = load_json(await github.read(), "GitHub")
        if slack is not None and slack.filename:
            sl = load_json(await slack.read(), "Slack")
        if jira is not None and jira.filename:
            jr = read_jira_csv(await jira.read())
        with get_conn() as c:
            pid = build_project(c, name.strip(), leaver.strip(), gh, sl, jr)
            log_event(c, pid, role, "project_created", summary="Dossier built from uploaded exports")
    except IngestError as e:
        raise HTTPException(400, str(e))
    return {"id": pid}


@app.get("/api/projects/{pid}")
def dossier(pid: int, role: str = Depends(get_role)):
    with get_conn() as c:
        p = project_or_404(c, pid)
        if role in RES and p["status"] != "locked":
            raise HTTPException(403, "The handover has not been released yet. The outgoing contributor must approve and lock it first.")
        scope = scope_set(c, pid) if role == "contractor" else None
        rows = c.execute("SELECT c.*, s.type AS s_type, s.ref AS s_ref FROM claims c "
                         "LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? ORDER BY c.id", (pid,)).fetchall()
        buckets = {b: [] for b in BUCKETS}
        for r in rows:
            if visible(r, role, scope):
                buckets[r["bucket"]].append(claim_view(r, role))
        out = {"project": {k: p[k] for k in ("id", "name", "leaver", "status", "as_of", "created_at", "locked_at")},
               "role": role, "buckets": buckets, "debrief": [], "redactions": [], "stats": None}
        if role == "contractor":
            out["scope"] = sorted(scope)
        if role not in RES:
            out["debrief"] = [dict(d) for d in c.execute(
                "SELECT idx,question,bucket,answer FROM debrief WHERE project_id=? ORDER BY idx", (pid,))]
            out["redactions"] = [dict(r) for r in c.execute(
                "SELECT kind,location FROM redactions WHERE project_id=?", (pid,))]
            counts = {"pending": 0, "approved": 0, "rejected": 0}
            for r in rows:
                counts[r["status"]] += 1
            groups = {r["conflict_group"] for r in rows if r["trust"] == "CONFLICT" and r["status"] == "pending"}
            out["stats"] = {**counts, "total": len(rows), "open_conflicts": len(groups),
                            "dropped_ungrounded": p["dropped"],
                            "debrief_answered": sum(1 for d in out["debrief"] if (d["answer"] or "").strip())}
        return out


@app.get("/api/sources/{sid}")
def source(sid: int, role: str = Depends(get_role)):
    with get_conn() as c:
        s = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "Source not found")
        p = project_or_404(c, s["project_id"])
        claims = c.execute("SELECT * FROM claims WHERE source_id=?", (sid,)).fetchall()
        if role in RES:
            if p["status"] != "locked":
                raise HTTPException(403, "The handover has not been released yet")
            scope = scope_set(c, p["id"]) if role == "contractor" else None
            claims = [x for x in claims if visible(x, role, scope)]
            if not claims:
                raise HTTPException(403, "This source is not part of the released handover")
        return {"id": s["id"], "type": s["type"], "ref": s["ref"], "ts": s["ts"], "author": s["author"],
                "text": s["text"], "meta": json.loads(s["meta"] or "{}"),
                "highlights": [x["quote"] for x in claims if x["quote"]]}


class DebriefIn(BaseModel):
    answers: dict


@app.post("/api/projects/{pid}/debrief")
def save_debrief(pid: int, body: DebriefIn, role: str = Depends(get_role)):
    with get_conn() as c:
        p = project_or_404(c, pid)
        if role != "leaver":
            raise HTTPException(403, "Only the outgoing contributor can answer the debrief")
        if p["status"] == "locked":
            raise HTTPException(409, "The dossier is locked")
        saved = 0
        for k, ans in body.answers.items():
            ans = (ans or "").strip()
            d = c.execute("SELECT * FROM debrief WHERE project_id=? AND idx=?", (pid, int(k))).fetchone()
            if not d:
                continue
            ref = f"Q{d['idx']}"
            old = c.execute("SELECT id FROM sources WHERE project_id=? AND type='debrief' AND ref=?", (pid, ref)).fetchone()
            c.execute("UPDATE debrief SET answer=? WHERE id=?", (ans, d["id"]))
            if not ans:
                if old:
                    c.execute("DELETE FROM claims WHERE source_id=?", (old["id"],))
                    c.execute("DELETE FROM sources WHERE id=?", (old["id"],))
                continue
            if old:
                existing = c.execute("SELECT id, summary FROM claims WHERE source_id=?", (old["id"],)).fetchone()
                if existing:
                    # Same answer re-saved: keep the claim and its approval. Changed answer: back to pending review.
                    if (existing["summary"] or "").strip() != ans:
                        c.execute("UPDATE claims SET summary=?, quote=?, edited_text=NULL, status='pending', ts=? WHERE id=?",
                                  (ans, ans, now(), existing["id"]))
                        c.execute("UPDATE sources SET text=?, ts=? WHERE id=?", (f"Q: {d['question']}\nA: {ans}", now(), old["id"]))
                    saved += 1
                    continue
                c.execute("DELETE FROM sources WHERE id=?", (old["id"],))
            sid = c.execute("INSERT INTO sources(project_id,type,ref,ts,author,text,meta) VALUES(?,?,?,?,?,?,?)",
                            (pid, "debrief", ref, now(), p["leaver"], f"Q: {d['question']}\nA: {ans}", "{}")).lastrowid
            c.execute("INSERT INTO claims(project_id,bucket,trust,component,attribute,value,summary,ts,source_id,quote)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (pid, d["bucket"], "STATED BY LEAVER", "platform", "debrief", ref, ans, now(), sid, ans))
            saved += 1
        log_event(c, pid, role, "debrief_saved", summary=f"Saved {saved} debrief answer(s)")
        return {"saved": saved}


class ActionIn(BaseModel):
    action: str
    text: Optional[str] = None
    visibility: Optional[str] = None


@app.post("/api/claims/{cid}/action")
def claim_action(cid: int, body: ActionIn, role: str = Depends(get_role)):
    with get_conn() as c:
        cl = load_claim(c, cid)
        p = leaver_edit_guard(c, cl, role)
        a = body.action
        if a == "approve":
            if cl["trust"] == "CONFLICT" and cl["status"] == "pending":
                raise HTTPException(400, "Resolve the conflict first by choosing the current value")
            if cl["trust"] == "MISSING EVIDENCE":
                raise HTTPException(400, "Provide an answer (edit) or reject this gap")
            c.execute("UPDATE claims SET status='approved' WHERE id=?", (cid,))
            log_event(c, p["id"], role, "approve", cl, f"Approved: {claim_text(cl)}", cl["status"], "approved")
        elif a == "reject":
            c.execute("UPDATE claims SET status='rejected' WHERE id=?", (cid,))
            log_event(c, p["id"], role, "reject", cl, f"Rejected: {claim_text(cl)}", cl["status"], "rejected")
        elif a == "reset":
            c.execute("UPDATE claims SET status='pending' WHERE id=?", (cid,))
            log_event(c, p["id"], role, "reset", cl, f"Undid decision on: {claim_text(cl)}", cl["status"], "pending")
        elif a == "visibility":
            if body.visibility not in ("all", "manager"):
                raise HTTPException(400, "visibility must be 'all' or 'manager'")
            c.execute("UPDATE claims SET visibility=? WHERE id=?", (body.visibility, cid))
            log_event(c, p["id"], role, "visibility", cl, f"Visibility changed on: {claim_text(cl)}", cl["visibility"], body.visibility)
        elif a == "edit":
            text = (body.text or "").strip()
            if not text:
                raise HTTPException(400, "Text is required")
            if cl["trust"] == "CONFLICT" and cl["status"] == "pending":
                raise HTTPException(400, "Resolve the conflict first")
            if cl["trust"] == "MISSING EVIDENCE":
                sid = c.execute("INSERT INTO sources(project_id,type,ref,ts,author,text,meta) VALUES(?,?,?,?,?,?,?)",
                                (p["id"], "debrief", f"Leaver note #{cid}", now(), p["leaver"], text, "{}")).lastrowid
                c.execute("UPDATE claims SET trust='STATED BY LEAVER', source_id=?, quote=?, edited_text=?, status='approved', "
                          "gap_state='answered' WHERE id=?", (sid, text, text, cid))
                log_event(c, p["id"], role, "answer_gap", cl, f"Answered gap: {claim_text(cl)}", cl["summary"], text)
            else:
                old_text = cl["edited_text"] or cl["summary"]
                c.execute("UPDATE claims SET edited_text=?, status='approved' WHERE id=?", (text, cid))
                log_event(c, p["id"], role, "edit", cl, "Edited a claim", old_text, text)
        else:
            raise HTTPException(400, "Unknown action")
    return {"ok": True}


class ResolveIn(BaseModel):
    mode: str  # winner | unknown


@app.post("/api/claims/{cid}/resolve")
def resolve(cid: int, body: ResolveIn, role: str = Depends(get_role)):
    with get_conn() as c:
        cl = load_claim(c, cid)
        p = leaver_edit_guard(c, cl, role)
        if not cl["conflict_group"]:
            raise HTTPException(400, "This claim is not part of a conflict")
        group = c.execute("SELECT * FROM claims WHERE project_id=? AND conflict_group=?",
                          (p["id"], cl["conflict_group"])).fetchall()
        if body.mode == "winner":
            for g in group:
                if g["id"] == cid:
                    c.execute("UPDATE claims SET status='approved', trust='SOURCE-BACKED' WHERE id=?", (cid,))
                else:
                    c.execute("UPDATE claims SET status='rejected' WHERE id=?", (g["id"],))
            others = ", ".join(sorted({g["value"] for g in group if g["id"] != cid}))
            log_event(c, p["id"], role, "resolve_conflict", cl,
                      f"Chose {cl['value']} as current for {cl['conflict_group']} (rejected: {others})", others, cl["value"])
        elif body.mode == "unknown":
            vals = ", ".join(sorted({g["value"] for g in group}))
            for g in group:
                c.execute("UPDATE claims SET status='rejected' WHERE id=?", (g["id"],))
            c.execute("INSERT INTO claims(project_id,bucket,trust,component,attribute,summary,ts,status,routed_to)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (p["id"], "gaps", "MISSING EVIDENCE", cl["component"], cl["attribute"],
                       f"Current {cl['attribute']} for {cl['component']} is unknown: sources disagree ({vals}).",
                       now(), "pending", "manager"))
            log_event(c, p["id"], role, "mark_unknown", cl, f"Marked {cl['conflict_group']} as unknown ({vals})")
        else:
            raise HTTPException(400, "mode must be 'winner' or 'unknown'")
    return {"ok": True}


@app.post("/api/projects/{pid}/approve-all")
def approve_all(pid: int, role: str = Depends(get_role)):
    with get_conn() as c:
        p = project_or_404(c, pid)
        if role != "leaver":
            raise HTTPException(403, "Only the outgoing contributor can approve claims")
        if p["status"] == "locked":
            raise HTTPException(409, "The dossier is locked")
        n = c.execute("UPDATE claims SET status='approved' WHERE project_id=? AND status='pending' "
                      "AND trust NOT IN ('CONFLICT','MISSING EVIDENCE')", (pid,)).rowcount
        log_event(c, pid, role, "approve_all", summary=f"Bulk-approved {n} pending claim(s)")
        return {"approved": n}


@app.post("/api/projects/{pid}/lock")
def lock(pid: int, role: str = Depends(get_role)):
    with get_conn() as c:
        p = project_or_404(c, pid)
        if role != "leaver":
            raise HTTPException(403, "Only the outgoing contributor can release the handover")
        if p["status"] == "locked":
            raise HTTPException(409, "Already locked")
        open_c = c.execute("SELECT COUNT(DISTINCT conflict_group) FROM claims WHERE project_id=? AND trust='CONFLICT' "
                           "AND status='pending'", (pid,)).fetchone()[0]
        if open_c:
            raise HTTPException(400, f"Resolve {open_c} open conflict(s) before locking")
        if not c.execute("SELECT 1 FROM claims WHERE project_id=? AND status='approved'", (pid,)).fetchone():
            raise HTTPException(400, "Approve at least one claim before locking")
        c.execute("UPDATE projects SET status='locked', locked_at=? WHERE id=?", (now(), pid))
        log_event(c, pid, role, "lock_release", summary="Dossier locked and released to the successor")
    return {"ok": True}


@app.get("/api/projects/{pid}/audit")
def audit(pid: int, role: str = Depends(get_role)):
    if role in RES:
        raise HTTPException(403, "The audit trail is visible to the outgoing contributor and managers")
    with get_conn() as c:
        project_or_404(c, pid)
        rows = c.execute("SELECT id,ts,actor,action,claim_id,summary,old,new FROM events WHERE project_id=? "
                         "ORDER BY id DESC", (pid,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Round 3: gap tracker, conflict radar, contractor scope, feedback loop, receipt
# ---------------------------------------------------------------------------
def managers_and_leavers(role, msg="Only the outgoing contributor or a manager can do this"):
    if role not in ("leaver", "manager"):
        raise HTTPException(403, msg)


class GapIn(BaseModel):
    owner: str
    due: Optional[str] = ""


@app.post("/api/claims/{cid}/gap")
def assign_gap(cid: int, body: GapIn, role: str = Depends(get_role)):
    managers_and_leavers(role, "Only the outgoing contributor or a manager can assign a gap")
    owner = (body.owner or "").strip()
    due = (body.due or "").strip()
    if not owner:
        raise HTTPException(400, "A gap needs a named owner")
    if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise HTTPException(400, "Due date must look like 2026-11-20")
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "That is not a real date")
    with get_conn() as c:
        cl = load_claim(c, cid)
        g = gap_of(cl)
        if not g or g["state"] not in ("open", "assigned"):
            raise HTTPException(400, "This item is not an open gap")
        c.execute("UPDATE claims SET gap_state='assigned', gap_owner=?, gap_due=? WHERE id=?", (owner, due or None, cid))
        log_event(c, cl["project_id"], role, "gap_assign", cl, f"Assigned gap '{cl['attribute']}' to {owner}"
                  + (f" (due {due})" if due else ""), g["owner"] or None, owner)
    return {"ok": True}


@app.post("/api/claims/{cid}/verify")
def verify_gap(cid: int, role: str = Depends(get_role)):
    if role != "manager":
        raise HTTPException(403, "Only a manager can verify an answer")
    with get_conn() as c:
        cl = load_claim(c, cid)
        g = gap_of(cl)
        if not g or g["state"] != "answered":
            raise HTTPException(400, "Only an answered gap can be verified")
        c.execute("UPDATE claims SET gap_state='verified' WHERE id=?", (cid,))
        log_event(c, cl["project_id"], role, "gap_verify", cl, f"Verified answer for gap '{cl['attribute']}'", "answered", "verified")
    return {"ok": True}


@app.get("/api/projects/{pid}/radar")
def radar(pid: int, role: str = Depends(get_role)):
    if role in RES:
        raise HTTPException(403, "The conflict radar is visible to the outgoing contributor and managers")
    with get_conn() as c:
        p = project_or_404(c, pid)
        rows = c.execute("SELECT c.*, s.type AS s_type, s.ref AS s_ref, s.ts AS s_ts FROM claims c "
                         "LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.conflict_group IS NOT NULL "
                         "ORDER BY c.id", (pid,)).fetchall()
        sources = c.execute("SELECT id,type,ref,ts,text FROM sources WHERE project_id=? AND type!='debrief' ORDER BY ts",
                            (pid,)).fetchall()
        by_group = {}
        for r in rows:
            by_group.setdefault(r["conflict_group"], []).append(r)
        groups = []
        for name, items in by_group.items():
            values = []
            for r in sorted(items, key=lambda x: (x["ts"] or "", x["id"])):
                if r["value"] not in values:
                    values.append(r["value"])
            points, claim_pairs = [], set()
            for r in items:
                if not r["source_id"]:
                    continue
                claim_pairs.add((r["source_id"], r["value"]))
                points.append({"kind": "claim", "id": r["id"], "ts": r["ts"] or r["s_ts"] or "", "value": r["value"],
                               "status": r["status"], "quote": r["quote"],
                               "source": {"id": r["source_id"], "type": r["s_type"], "ref": r["s_ref"]}})
            for v in values:
                pat = re.compile(r"\b" + re.escape(v) + r"\b", re.I)
                for s in sources:
                    if not s["ts"] or (s["id"], v) in claim_pairs:
                        continue
                    m = pat.search(s["text"] or "")
                    if not m:
                        continue
                    a, b = max(0, m.start() - 50), min(len(s["text"]), m.end() + 50)
                    snippet = re.sub(r"\s+", " ", s["text"][a:b]).strip()
                    points.append({"kind": "mention", "ts": s["ts"], "value": v, "snippet": snippet,
                                   "source": {"id": s["id"], "type": s["type"], "ref": s["ref"]}})
            points.sort(key=lambda x: (x["ts"], x["kind"]))
            approved = [r for r in items if r["status"] == "approved"]
            pending = [r for r in items if r["trust"] == "CONFLICT" and r["status"] == "pending"]
            state = "resolved" if approved else "open" if pending else "unknown"
            groups.append({"group": name, "component": items[0]["component"], "attribute": items[0]["attribute"],
                           "values": values, "points": points, "state": state,
                           "winner": approved[0]["value"] if approved else None})
        return {"as_of": p["as_of"], "groups": groups}


# ----- contractor scope -----
class ScopeIn(BaseModel):
    components: list[str]


@app.get("/api/projects/{pid}/scope")
def get_scope(pid: int, role: str = Depends(get_role)):
    managers_and_leavers(role, "Only the outgoing contributor or a manager can see contractor access")
    with get_conn() as c:
        project_or_404(c, pid)
        comps = [r["component"] for r in c.execute(
            "SELECT DISTINCT component FROM claims WHERE project_id=? AND component IS NOT NULL AND component!='' "
            "ORDER BY component", (pid,))]
        return {"components": comps, "selected": sorted(scope_set(c, pid))}


@app.post("/api/projects/{pid}/scope")
def set_scope(pid: int, body: ScopeIn, role: str = Depends(get_role)):
    managers_and_leavers(role, "Only the outgoing contributor or a manager can grant contractor access")
    with get_conn() as c:
        project_or_404(c, pid)
        valid = {r["component"] for r in c.execute("SELECT DISTINCT component FROM claims WHERE project_id=?", (pid,))}
        new = sorted({x for x in body.components if x in valid})
        if len(new) != len(set(body.components)):
            raise HTTPException(400, "Unknown component in the list")
        old = sorted(scope_set(c, pid))
        c.execute("DELETE FROM scope WHERE project_id=?", (pid,))
        for comp in new:
            c.execute("INSERT INTO scope(project_id, component) VALUES(?,?)", (pid, comp))
        if old != new:
            log_event(c, pid, role, "scope_changed",
                      summary="Contractor access: " + (", ".join(new) if new else "none"), old=", ".join(old) or "none",
                      new=", ".join(new) or "none")
        return {"components": new}


# ----- feedback loop -----
FB_KINDS = ("wrong", "unclear", "missing")


class FeedbackIn(BaseModel):
    kind: str
    note: str
    claim_id: Optional[int] = None


def fb_view(c, r):
    text = None
    if r["claim_id"]:
        cl = c.execute("SELECT edited_text, summary FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
        text = (cl["edited_text"] or cl["summary"]) if cl else None
    return {"id": r["id"], "claim_id": r["claim_id"], "claim_text": text, "kind": r["kind"], "note": r["note"],
            "author": r["author"], "status": r["status"], "created": r["created"], "reply": r["reply"],
            "replied_by": r["replied_by"]}


@app.get("/api/projects/{pid}/feedback")
def list_feedback(pid: int, role: str = Depends(get_role)):
    if role == "contractor":
        raise HTTPException(403, "Contractors cannot use the feedback loop")
    with get_conn() as c:
        project_or_404(c, pid)
        rows = c.execute("SELECT * FROM feedback WHERE project_id=? ORDER BY id", (pid,)).fetchall()
        return [fb_view(c, r) for r in rows]


@app.post("/api/projects/{pid}/feedback")
def add_feedback(pid: int, body: FeedbackIn, role: str = Depends(get_role)):
    if role != "successor":
        raise HTTPException(403, "Only the successor can raise feedback on a released handover")
    note = (body.note or "").strip()
    if body.kind not in FB_KINDS:
        raise HTTPException(400, "kind must be wrong, unclear or missing")
    if not note:
        raise HTTPException(400, "Write a short note first")
    with get_conn() as c:
        p = project_or_404(c, pid)
        if p["status"] != "locked":
            raise HTTPException(409, "The handover has not been released yet")
        cl = None
        if body.claim_id:
            cl = c.execute("SELECT * FROM claims WHERE id=? AND project_id=?", (body.claim_id, pid)).fetchone()
            if not cl or not visible(cl, "successor"):
                raise HTTPException(400, "That claim is not part of the released handover")
        fid = c.execute("INSERT INTO feedback(project_id,claim_id,kind,note,author,status,created) VALUES(?,?,?,?,?,?,?)",
                        (pid, body.claim_id, body.kind, note, role, "open", now())).lastrowid
        log_event(c, pid, role, "feedback", cl, f"Successor flagged as {body.kind}: {note[:100]}")
    return {"id": fid}


class ReplyIn(BaseModel):
    text: str


def load_feedback(c, fid):
    r = c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
    if not r:
        raise HTTPException(404, "Feedback not found")
    return r


@app.post("/api/feedback/{fid}/reply")
def reply_feedback(fid: int, body: ReplyIn, role: str = Depends(get_role)):
    managers_and_leavers(role, "Only the outgoing contributor or a manager can answer feedback")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Reply text is required")
    with get_conn() as c:
        f = load_feedback(c, fid)
        if f["status"] == "closed":
            raise HTTPException(409, "This thread is closed")
        c.execute("UPDATE feedback SET reply=?, replied_by=?, replied_at=?, status='answered' WHERE id=?",
                  (text, role, now(), fid))
        log_event(c, f["project_id"], role, "feedback_reply", None, f"Answered feedback #{fid}: {text[:100]}")
    return {"ok": True}


@app.post("/api/feedback/{fid}/close")
def close_feedback(fid: int, role: str = Depends(get_role)):
    if role != "successor":
        raise HTTPException(403, "Only the successor can accept an answer")
    with get_conn() as c:
        f = load_feedback(c, fid)
        if f["status"] != "answered":
            raise HTTPException(409, "There is no answer to accept yet" if f["status"] == "open" else "Already closed")
        c.execute("UPDATE feedback SET status='closed' WHERE id=?", (fid,))
        log_event(c, f["project_id"], role, "feedback_closed", None, f"Accepted the answer to feedback #{fid}")
    return {"ok": True}


# ----- receipt: acknowledgment + manager co-sign -----
def fingerprint(c, pid):
    rows = c.execute("SELECT c.id, c.bucket, c.trust, c.component, c.edited_text, c.summary, c.quote, s.ref AS ref "
                     "FROM claims c LEFT JOIN sources s ON s.id=c.source_id "
                     "WHERE c.project_id=? AND c.status='approved' AND c.visibility='all' ORDER BY c.id", (pid,)).fetchall()
    payload = [[r["id"], r["bucket"], r["trust"], r["component"], r["edited_text"] or r["summary"], r["quote"], r["ref"]]
               for r in rows]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest, len(rows)


def get_receipt_row(c, pid):
    c.execute("INSERT OR IGNORE INTO receipts(project_id) VALUES(?)", (pid,))
    return c.execute("SELECT * FROM receipts WHERE project_id=?", (pid,)).fetchone()


@app.get("/api/projects/{pid}/receipt")
def receipt(pid: int, role: str = Depends(get_role)):
    if role == "contractor":
        raise HTTPException(403, "Contractors do not receive the handover receipt")
    with get_conn() as c:
        p = project_or_404(c, pid)
        rc = get_receipt_row(c, pid)
        digest, n = fingerprint(c, pid)
        if p["status"] != "locked":
            status = "draft"
        elif rc["m_ack_at"]:
            status = "completed"
        elif rc["s_ack_at"]:
            status = "acknowledged"
        else:
            status = "released"
        open_fb = c.execute("SELECT COUNT(*) FROM feedback WHERE project_id=? AND status='open'", (pid,)).fetchone()[0]
        return {"project": p["name"], "leaver": p["leaver"], "status": status, "locked_at": p["locked_at"],
                "claims_released": n, "fingerprint": digest, "s_ack_at": rc["s_ack_at"], "s_note": rc["s_note"],
                "m_ack_at": rc["m_ack_at"], "m_note": rc["m_note"], "open_feedback": open_fb}


class NoteIn(BaseModel):
    text: Optional[str] = ""


@app.post("/api/projects/{pid}/acknowledge")
def acknowledge(pid: int, body: NoteIn, role: str = Depends(get_role)):
    if role != "successor":
        raise HTTPException(403, "Only the successor can acknowledge receipt")
    with get_conn() as c:
        p = project_or_404(c, pid)
        if p["status"] != "locked":
            raise HTTPException(409, "The handover has not been released yet")
        rc = get_receipt_row(c, pid)
        if rc["s_ack_at"]:
            raise HTTPException(409, "Receipt already acknowledged")
        if c.execute("SELECT 1 FROM feedback WHERE project_id=? AND status='open'", (pid,)).fetchone():
            raise HTTPException(400, "Your feedback is still waiting for an answer. Wait for the reply before acknowledging.")
        note = (body.text or "").strip()
        c.execute("UPDATE receipts SET s_ack_at=?, s_note=? WHERE project_id=?", (now(), note or None, pid))
        log_event(c, pid, role, "acknowledge", summary="Successor acknowledged receipt of the handover"
                  + (f": {note[:100]}" if note else ""))
    return {"ok": True}


@app.post("/api/projects/{pid}/cosign")
def cosign(pid: int, body: NoteIn, role: str = Depends(get_role)):
    if role != "manager":
        raise HTTPException(403, "Only a manager can co-sign the handover")
    with get_conn() as c:
        p = project_or_404(c, pid)
        if p["status"] != "locked":
            raise HTTPException(409, "The handover has not been released yet")
        rc = get_receipt_row(c, pid)
        if not rc["s_ack_at"]:
            raise HTTPException(400, "The successor has not acknowledged receipt yet")
        if rc["m_ack_at"]:
            raise HTTPException(409, "Already co-signed")
        note = (body.text or "").strip()
        c.execute("UPDATE receipts SET m_ack_at=?, m_note=? WHERE project_id=?", (now(), note or None, pid))
        log_event(c, pid, role, "cosign", summary="Manager co-signed the handover"
                  + (f": {note[:100]}" if note else ""))
    return {"ok": True}