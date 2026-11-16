import os
import tempfile

os.environ["RELAYTRACE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.demo_data import FAKE_AWS, FAKE_PW, FAKE_STRIPE  # noqa: E402
from app.main import PINS, app, make_token  # noqa: E402


def H(role):
    return {"X-Token": make_token(role)}


def test_full_workflow():
    with TestClient(app) as cl:
        pid = cl.post("/api/demo", headers=H("leaver")).json()["id"]
        d = cl.get(f"/api/projects/{pid}", headers=H("leaver")).json()
        b = d["buckets"]

        # claims exist in every bucket and every non-gap claim cites a source with a quote
        assert all(len(b[k]) > 0 for k in ("decisions", "unfinished", "risks", "contacts", "gaps"))
        for k, items in b.items():
            for c in items:
                if c["trust"] != "MISSING EVIDENCE":
                    assert c["source"] and c["quote"]

        # planted Redis vs Kafka conflict
        conf = [c for c in b["decisions"] if c["trust"] == "CONFLICT"]
        assert {c["value"] for c in conf} == {"Redis", "Kafka"}
        assert [c["value"] for c in conf if c["suggested"]] == ["Kafka"]

        # dormant branches, bus factor, contacts, gaps
        assert any("dormant for 56 days" in c["text"] for c in b["unfinished"])  # calendar days, not 55
        assert any("(in PAY-151" in c["text"] for c in b["unfinished"])  # blockers keep their context
        assert any("reconcile.py" in c["text"] for c in b["risks"])
        assert any("priya reviewed 14" in c["text"] for c in b["contacts"])
        assert any(c["attribute"] == "deployment ownership" for c in b["gaps"])

        # secrets never persisted anywhere
        dump = cl.get(f"/api/projects/{pid}", headers=H("manager")).text
        for s in (FAKE_AWS, FAKE_PW, FAKE_STRIPE):
            assert s not in dump
        assert len(d["redactions"]) >= 3
        ids = [c["source"]["id"] for items in b.values() for c in items if c["source"]]
        for sid in set(ids):
            assert not any(s in cl.get(f"/api/sources/{sid}", headers=H("leaver")).text for s in (FAKE_AWS, FAKE_PW, FAKE_STRIPE))

        # successor blocked before release
        assert cl.get(f"/api/projects/{pid}", headers=H("successor")).status_code == 403
        # cannot lock with open conflict; successor cannot edit
        assert cl.post(f"/api/projects/{pid}/lock", headers=H("leaver")).status_code == 400
        assert cl.post(f"/api/claims/{conf[0]['id']}/resolve", json={"mode": "winner"}, headers=H("successor")).status_code == 403

        # resolve, answer debrief, fill a gap, approve all, hide one claim from successors, lock
        kafka = next(c for c in conf if c["value"] == "Kafka")
        assert cl.post(f"/api/claims/{kafka['id']}/resolve", json={"mode": "winner"}, headers=H("leaver")).status_code == 200
        assert cl.post(f"/api/projects/{pid}/debrief", json={"answers": {"1": "Idle because ledger entries are not idempotent."}},
                       headers=H("leaver")).json()["saved"] == 1
        gap = b["gaps"][0]
        assert cl.post(f"/api/claims/{gap['id']}/action", json={"action": "edit", "text": "Karan owns deployments."},
                       headers=H("leaver")).status_code == 200
        assert cl.post(f"/api/projects/{pid}/approve-all", headers=H("leaver")).json()["approved"] > 0
        secret_claim = b["contacts"][0]
        cl.post(f"/api/claims/{secret_claim['id']}/action", json={"action": "visibility", "visibility": "manager"}, headers=H("leaver"))
        assert cl.post(f"/api/projects/{pid}/lock", headers=H("leaver")).status_code == 200
        assert cl.post(f"/api/claims/{kafka['id']}/action", json={"action": "reject"}, headers=H("leaver")).status_code == 409

        s = cl.get(f"/api/projects/{pid}", headers=H("successor")).json()
        flat = [c for items in s["buckets"].values() for c in items]
        assert flat and all(c["status"] == "approved" and c["visibility"] == "all" for c in flat)
        assert secret_claim["id"] not in [c["id"] for c in flat]
        assert any(c["trust"] == "STATED BY LEAVER" for c in flat)
        assert s["redactions"] == [] and s["debrief"] == []
        sid = next(c["source"]["id"] for c in flat if c["source"])
        assert cl.get(f"/api/sources/{sid}", headers=H("successor")).status_code == 200

        # audit trail: every decision is recorded, readable by leaver/manager, hidden from the successor
        ev = cl.get(f"/api/projects/{pid}/audit", headers=H("manager")).json()
        actions = [e["action"] for e in ev]
        for a in ("project_created", "resolve_conflict", "debrief_saved", "answer_gap", "approve_all", "visibility", "lock_release"):
            assert a in actions, a
        vis = next(e for e in ev if e["action"] == "visibility")
        assert (vis["old"], vis["new"]) == ("all", "manager") and vis["actor"] == "leaver"
        assert ev[0]["action"] == "lock_release"  # newest first
        assert cl.get(f"/api/projects/{pid}/audit", headers=H("successor")).status_code == 403


def test_bad_uploads():
    with TestClient(app) as cl:
        assert cl.post("/api/ingest", data={"name": "x"}, headers=H("leaver")).status_code == 400
        r = cl.post("/api/ingest", data={"name": "x"}, files={"github": ("g.json", b"not json")}, headers=H("leaver"))
        assert r.status_code == 400
        r = cl.post("/api/ingest", data={"name": "x"}, files={"slack": ("s.json", b"")}, headers=H("leaver"))
        assert r.status_code == 400
        r = cl.post("/api/ingest", data={"name": "x"}, files={"jira": ("j.csv", b"a,b\n1,2\n")}, headers=H("leaver"))
        assert r.status_code == 400


def test_sample_files_roundtrip():
    with TestClient(app) as cl:
        files = {}
        for n in ("github.json", "slack.json", "jira.csv"):
            files[n.split(".")[0]] = (n, cl.get(f"/api/demo-files/{n}").content)
        r = cl.post("/api/ingest", data={"name": "uploaded", "leaver": "arjun"}, files=files, headers=H("leaver"))
        assert r.status_code == 200
        d = cl.get(f"/api/projects/{r.json()['id']}", headers=H("leaver")).json()
        assert d["stats"]["total"] > 10


def test_sign_in():
    with TestClient(app) as cl:
        assert cl.get("/api/projects").status_code == 401
        assert cl.get("/api/projects", headers={"X-Token": "leaver.forged"}).status_code == 401
        assert cl.post("/api/login", json={"role": "leaver", "pin": "0000"}).status_code == 401
        r = cl.post("/api/login", json={"role": "leaver", "pin": PINS["leaver"]})
        assert r.status_code == 200
        assert cl.get("/api/projects", headers={"X-Token": r.json()["token"]}).status_code == 200
        # a successor token cannot create projects
        assert cl.post("/api/demo", headers=H("successor")).status_code == 403

def _demo(cl):
    pid = cl.post("/api/demo", headers=H("leaver")).json()["id"]
    return pid, cl.get(f"/api/projects/{pid}", headers=H("leaver")).json()["buckets"]


def _release(cl, pid, b, scope=None):
    """Resolve the conflict, answer one gap, approve everything and lock."""
    kafka = next(c for c in b["decisions"] if c["trust"] == "CONFLICT" and c["value"] == "Kafka")
    assert cl.post(f"/api/claims/{kafka['id']}/resolve", json={"mode": "winner"}, headers=H("leaver")).status_code == 200
    if scope is not None:
        assert cl.post(f"/api/projects/{pid}/scope", json={"components": scope}, headers=H("leaver")).status_code == 200
    cl.post(f"/api/projects/{pid}/approve-all", headers=H("leaver"))
    assert cl.post(f"/api/projects/{pid}/lock", headers=H("leaver")).status_code == 200


def test_radar_and_gap_tracker():
    with TestClient(app) as cl:
        pid, b = _demo(cl)
        r = cl.get(f"/api/projects/{pid}/radar", headers=H("manager")).json()
        g = r["groups"][0]
        assert g["state"] == "open" and set(g["values"]) == {"Redis", "Kafka"} and r["as_of"]
        kinds = {p["kind"] for p in g["points"]}
        assert kinds == {"claim", "mention"}
        assert all(p["source"]["id"] and p["ts"] for p in g["points"])
        assert cl.get(f"/api/projects/{pid}/radar", headers=H("successor")).status_code == 403
        assert cl.get(f"/api/projects/{pid}/radar", headers=H("contractor")).status_code == 403
        kafka = next(c for c in b["decisions"] if c["value"] == "Kafka")
        cl.post(f"/api/claims/{kafka['id']}/resolve", json={"mode": "winner"}, headers=H("leaver"))
        g = cl.get(f"/api/projects/{pid}/radar", headers=H("leaver")).json()["groups"][0]
        assert g["state"] == "resolved" and g["winner"] == "Kafka"

        # gap tracker: open -> assigned -> answered -> verified
        gap = b["gaps"][0]
        assert gap["gap"]["state"] == "open"
        assert cl.post(f"/api/claims/{gap['id']}/gap", json={"owner": "", "due": ""}, headers=H("manager")).status_code == 400
        assert cl.post(f"/api/claims/{gap['id']}/gap", json={"owner": "Karan", "due": "tomorrow"}, headers=H("manager")).status_code == 400
        assert cl.post(f"/api/claims/{gap['id']}/gap", json={"owner": "Karan", "due": "2026-11-20"}, headers=H("successor")).status_code == 403
        assert cl.post(f"/api/claims/{gap['id']}/gap", json={"owner": "Karan", "due": "2026-11-20"}, headers=H("manager")).status_code == 200
        assert cl.post(f"/api/claims/{gap['id']}/verify", headers=H("manager")).status_code == 400  # not answered yet
        cl.post(f"/api/claims/{gap['id']}/action", json={"action": "edit", "text": "Karan owns deployments."}, headers=H("leaver"))
        now_gap = next(c for c in cl.get(f"/api/projects/{pid}", headers=H("manager")).json()["buckets"]["gaps"] if c["id"] == gap["id"])
        assert now_gap["gap"] == {"state": "answered", "owner": "Karan", "due": "2026-11-20"}
        assert cl.post(f"/api/claims/{gap['id']}/verify", headers=H("leaver")).status_code == 403  # manager only
        assert cl.post(f"/api/claims/{gap['id']}/verify", headers=H("manager")).status_code == 200
        done = next(c for c in cl.get(f"/api/projects/{pid}", headers=H("manager")).json()["buckets"]["gaps"] if c["id"] == gap["id"])
        assert done["gap"]["state"] == "verified"
        ev = [e["action"] for e in cl.get(f"/api/projects/{pid}/audit", headers=H("manager")).json()]
        assert "gap_assign" in ev and "gap_verify" in ev


def test_contractor_scope():
    with TestClient(app) as cl:
        assert cl.post("/api/login", json={"role": "contractor", "pin": "8642"}).status_code == 200
        pid, b = _demo(cl)
        sc = cl.get(f"/api/projects/{pid}/scope", headers=H("leaver")).json()
        assert "webhooks" in sc["components"] and sc["selected"] == []
        assert cl.post(f"/api/projects/{pid}/scope", json={"components": ["nope"]}, headers=H("leaver")).status_code == 400
        assert cl.post(f"/api/projects/{pid}/scope", json={"components": ["webhooks"]}, headers=H("successor")).status_code == 403
        assert cl.get(f"/api/projects/{pid}", headers=H("contractor")).status_code == 403  # not released yet
        _release(cl, pid, b, scope=["webhooks"])
        d = cl.get(f"/api/projects/{pid}", headers=H("contractor")).json()
        flat = [c for items in d["buckets"].values() for c in items]
        assert flat and d["scope"] == ["webhooks"]
        assert all(c["component"] == "webhooks" and c["status"] == "approved" and c["visibility"] == "all" for c in flat)
        assert all("routed_to" not in c and "gap" not in c for c in flat)
        assert d["debrief"] == [] and d["redactions"] == [] and d["stats"] is None
        succ = [c for items in cl.get(f"/api/projects/{pid}", headers=H("successor")).json()["buckets"].values() for c in items]
        assert len(succ) > len(flat)
        # sources outside the scope stay closed; audit, radar, feedback, receipt are not for contractors
        other = next(c for c in succ if c["component"] != "webhooks" and c["source"])
        assert cl.get(f"/api/sources/{other['source']['id']}", headers=H("contractor")).status_code == 403
        assert cl.get(f"/api/sources/{flat[0]['source']['id']}", headers=H("contractor")).status_code == 200 if flat[0]["source"] else True
        for path in ("audit", "radar", "scope", "feedback", "receipt"):
            assert cl.get(f"/api/projects/{pid}/{path}", headers=H("contractor")).status_code == 403, path
        assert cl.post("/api/demo", headers=H("contractor")).status_code == 403
        # revoking access empties the view; the change is audited
        cl.post(f"/api/projects/{pid}/scope", json={"components": []}, headers=H("manager"))
        d = cl.get(f"/api/projects/{pid}", headers=H("contractor")).json()
        assert all(not items for items in d["buckets"].values()) and d["scope"] == []
        assert "scope_changed" in [e["action"] for e in cl.get(f"/api/projects/{pid}/audit", headers=H("leaver")).json()]


def test_feedback_receipt_and_cosign():
    with TestClient(app) as cl:
        pid, b = _demo(cl)
        r = cl.get(f"/api/projects/{pid}/receipt", headers=H("leaver")).json()
        assert r["status"] == "draft"
        _release(cl, pid, b)
        r = cl.get(f"/api/projects/{pid}/receipt", headers=H("successor")).json()
        assert r["status"] == "released" and len(r["fingerprint"]) == 64 and r["claims_released"] > 0
        flat = [c for items in cl.get(f"/api/projects/{pid}", headers=H("successor")).json()["buckets"].values() for c in items]
        assert r["claims_released"] == len(flat)

        # order of sign-off is enforced
        assert cl.post(f"/api/projects/{pid}/cosign", json={"text": ""}, headers=H("manager")).status_code == 400
        assert cl.post(f"/api/projects/{pid}/acknowledge", json={"text": ""}, headers=H("manager")).status_code == 403

        # feedback: validation, then successor-only, then it blocks acknowledgment until answered
        assert cl.post(f"/api/projects/{pid}/feedback", json={"kind": "bogus", "note": "x"}, headers=H("successor")).status_code == 400
        assert cl.post(f"/api/projects/{pid}/feedback", json={"kind": "wrong", "note": " "}, headers=H("successor")).status_code == 400
        assert cl.post(f"/api/projects/{pid}/feedback", json={"kind": "wrong", "note": "x"}, headers=H("leaver")).status_code == 403
        f1 = cl.post(f"/api/projects/{pid}/feedback", json={"kind": "wrong", "note": "Is Kafka really live?", "claim_id": flat[0]["id"]},
                     headers=H("successor")).json()["id"]
        f2 = cl.post(f"/api/projects/{pid}/feedback", json={"kind": "missing", "note": "Where is the runbook?"}, headers=H("successor")).json()["id"]
        fb = cl.get(f"/api/projects/{pid}/feedback", headers=H("leaver")).json()
        assert [x["status"] for x in fb] == ["open", "open"] and fb[0]["claim_text"] and fb[1]["claim_id"] is None
        assert cl.get(f"/api/projects/{pid}/receipt", headers=H("manager")).json()["open_feedback"] == 2
        assert cl.post(f"/api/projects/{pid}/acknowledge", json={"text": ""}, headers=H("successor")).status_code == 400
        assert cl.post(f"/api/feedback/{f1}/close", headers=H("successor")).status_code == 409  # no answer yet
        assert cl.post(f"/api/feedback/{f1}/reply", json={"text": "Yes, since Oct."}, headers=H("successor")).status_code == 403
        assert cl.post(f"/api/feedback/{f1}/reply", json={"text": "Yes, since Oct."}, headers=H("leaver")).status_code == 200
        assert cl.post(f"/api/feedback/{f2}/reply", json={"text": "In the wiki."}, headers=H("manager")).status_code == 200
        assert cl.post(f"/api/feedback/{f1}/close", headers=H("leaver")).status_code == 403
        assert cl.post(f"/api/feedback/{f1}/close", headers=H("successor")).status_code == 200
        assert cl.post(f"/api/feedback/{f1}/reply", json={"text": "late"}, headers=H("leaver")).status_code == 409

        fp = cl.get(f"/api/projects/{pid}/receipt", headers=H("successor")).json()["fingerprint"]
        assert cl.post(f"/api/projects/{pid}/acknowledge", json={"text": "Got it"}, headers=H("successor")).status_code == 200
        assert cl.post(f"/api/projects/{pid}/acknowledge", json={"text": ""}, headers=H("successor")).status_code == 409
        r = cl.get(f"/api/projects/{pid}/receipt", headers=H("manager")).json()
        assert r["status"] == "acknowledged" and r["s_note"] == "Got it" and r["s_ack_at"]
        assert cl.post(f"/api/projects/{pid}/cosign", json={"text": "ok"}, headers=H("successor")).status_code == 403
        assert cl.post(f"/api/projects/{pid}/cosign", json={"text": "Approved"}, headers=H("manager")).status_code == 200
        assert cl.post(f"/api/projects/{pid}/cosign", json={"text": ""}, headers=H("manager")).status_code == 409
        r = cl.get(f"/api/projects/{pid}/receipt", headers=H("leaver")).json()
        assert r["status"] == "completed" and r["m_note"] == "Approved" and r["fingerprint"] == fp
        ev = [e["action"] for e in cl.get(f"/api/projects/{pid}/audit", headers=H("manager")).json()]
        for a in ("feedback", "feedback_reply", "feedback_closed", "acknowledge", "cosign"):
            assert a in ev, a


def test_debrief_resave_keeps_approvals():
    with TestClient(app) as cl:
        pid, b = _demo(cl)
        ans = {"1": "Idle because ledger entries are not idempotent."}
        assert cl.post(f"/api/projects/{pid}/debrief", json={"answers": ans}, headers=H("leaver")).json()["saved"] == 1

        def mine():
            bk = cl.get(f"/api/projects/{pid}", headers=H("leaver")).json()["buckets"]
            return [c for items in bk.values() for c in items if c["trust"] == "STATED BY LEAVER"]
        before = mine()
        assert len(before) == 1
        cl.post(f"/api/claims/{before[0]['id']}/action", json={"action": "approve"}, headers=H("leaver"))
        # saving the same answer again must not reset the approval or create a duplicate
        assert cl.post(f"/api/projects/{pid}/debrief", json={"answers": ans}, headers=H("leaver")).json()["saved"] == 1
        after = mine()
        assert len(after) == 1 and after[0]["id"] == before[0]["id"] and after[0]["status"] == "approved"
        # a changed answer goes back to review; clearing it removes the claim
        ans2 = {"1": "Idle because settlement retries double-post."}
        cl.post(f"/api/projects/{pid}/debrief", json={"answers": ans2}, headers=H("leaver"))
        ch = mine()
        assert len(ch) == 1 and ch[0]["status"] == "pending" and "double-post" in ch[0]["text"]
        cl.post(f"/api/projects/{pid}/debrief", json={"answers": {"1": ""}}, headers=H("leaver"))
        assert mine() == []