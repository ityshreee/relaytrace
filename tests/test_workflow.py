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