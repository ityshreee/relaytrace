import hashlib
import hmac
import json
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

ROLES = ("leaver", "manager", "successor")
# Demo-grade sign-in: one PIN per role, overridable with environment variables on a real deployment.
PINS = {r: os.environ.get(f"RELAYTRACE_PIN_{r.upper()}", d)
        for r, d in (("leaver", "2468"), ("manager", "1357"), ("successor", "9753"))}
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


def visible(row, role):
    if role in ("leaver", "manager"):
        return True
    return row["status"] == "approved" and row["visibility"] == "all"


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
    if role != "successor":
        d["routed_to"] = r["routed_to"]
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
    if role == "successor":
        raise HTTPException(403, "Successors cannot create projects")


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
        if role == "successor" and p["status"] != "locked":
            raise HTTPException(403, "The handover has not been released yet. The outgoing contributor must approve and lock it first.")
        rows = c.execute("SELECT c.*, s.type AS s_type, s.ref AS s_ref FROM claims c "
                         "LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? ORDER BY c.id", (pid,)).fetchall()
        buckets = {b: [] for b in BUCKETS}
        for r in rows:
            if visible(r, role):
                buckets[r["bucket"]].append(claim_view(r, role))
        out = {"project": {k: p[k] for k in ("id", "name", "leaver", "status", "as_of", "created_at", "locked_at")},
               "role": role, "buckets": buckets, "debrief": [], "redactions": [], "stats": None}
        if role != "successor":
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
        if role == "successor":
            if p["status"] != "locked":
                raise HTTPException(403, "The handover has not been released yet")
            claims = [x for x in claims if visible(x, role)]
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
            if old:
                c.execute("DELETE FROM claims WHERE source_id=?", (old["id"],))
                c.execute("DELETE FROM sources WHERE id=?", (old["id"],))
            c.execute("UPDATE debrief SET answer=? WHERE id=?", (ans, d["id"]))
            if not ans:
                continue
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
                c.execute("UPDATE claims SET trust='STATED BY LEAVER', source_id=?, quote=?, edited_text=?, status='approved' "
                          "WHERE id=?", (sid, text, text, cid))
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
    if role == "successor":
        raise HTTPException(403, "The audit trail is visible to the outgoing contributor and managers")
    with get_conn() as c:
        project_or_404(c, pid)
        rows = c.execute("SELECT id,ts,actor,action,claim_id,summary,old,new FROM events WHERE project_id=? "
                         "ORDER BY id DESC", (pid,)).fetchall()
        return [dict(r) for r in rows]