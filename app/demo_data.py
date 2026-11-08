"""Synthetic PayFlow-Core transition dataset. Planted on purpose:
  * a Redis-vs-Kafka conflict on the webhook message broker (Slack, Aug vs Jira, Oct)
  * 2 dormant branches + 1 recent unmerged branch + 1 open PR
  * files only the leaver touched (bus factor)
  * leaked fake secrets (built by concatenation so source scanners don't flag this repo)
  * no mention of deployments / on-call / rollback -> MISSING EVIDENCE gaps
"""
import csv
import hashlib
import io
import json
import random
from datetime import datetime, timedelta, timezone

LEAVER = "arjun"
PROJECT_NAME = "PayFlow-Core (demo)"

# fake secrets assembled at runtime
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_STRIPE = "sk_" + "live_" + "51HxAbCdEfGhIjKlMnOp"
FAKE_PW = "hunter2" + "hunter2"


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def build_demo():
    rnd = random.Random(7)
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    commits, prs, branches = [], [], []

    def commit(author, when, msg, files, branch="main"):
        sha = _sha(f"{author}{when}{msg}")
        patch = "\n".join(
            f"--- a/{f}\n+++ b/{f}\n@@ -10,6 +10,9 @@\n def handle(ctx):\n-    return legacy(ctx)\n+    # {msg.splitlines()[0]}\n+    return handle_v2(ctx)"
            for f in files)
        commits.append({"sha": sha, "author": author, "date": _iso(when), "message": msg, "files": files,
                        "branch": branch, "patch": patch})
        return sha

    def pr(n, author, when, title, body, reviewers, files, state="closed", merged=True, comments=None, branch=""):
        prs.append({"number": n, "title": title, "body": body, "author": author, "created_at": _iso(when),
                    "state": state, "merged": merged, "reviewers": reviewers, "files": files,
                    "comments": comments or [], "branch": branch})

    auth_topics = ["session expiry fix", "refresh token rotation", "rate limit login", "password reset flow",
                   "OAuth callback validation", "MFA enrollment", "session cookie flags", "audit log for logins",
                   "fix CSRF on callback", "API key hashing", "lockout policy", "SSO metadata parsing",
                   "token introspection endpoint", "remove legacy login route"]
    for i, topic in enumerate(auth_topics):
        when = base + timedelta(days=i * 8 + rnd.randint(0, 3))
        files = ["auth/session.py", "auth/tokens.py"] if i % 2 == 0 else ["auth/session.py", "tests/test_auth.py"]
        commit(LEAVER, when, f"auth: {topic}", files)
        if i % 2 == 0:
            commit("priya", when + timedelta(hours=5), f"auth: review fixes for {topic}", ["auth/session.py"])
        pr(20 + i, LEAVER, when, f"auth: {topic}", f"Implements {topic}.", ["priya"], files,
           comments=[{"author": "priya", "created_at": _iso(when + timedelta(hours=3)), "body": "LGTM, thanks."}] if i == 0 else None)

    for i in range(6):
        when = base + timedelta(days=10 + i * 17)
        commit(LEAVER, when, f"payments: reconcile job update {i + 1}", ["payments/reconcile.py", "payments/payout.py"])
        if i % 2:
            commit("karan", when + timedelta(hours=4), f"payments: payout tweak {i}", ["payments/payout.py"])
        body = "Reconcile cron update."
        if i == 2:
            body = "Decided to cap payout retries at 5 with exponential backoff."
        if i == 4:
            body = f"Staging config for the sandbox: STRIPE_SECRET_KEY={FAKE_STRIPE}"
        pr(40 + i, LEAVER, when, f"payments: reconcile job update {i + 1}", body, ["karan"],
           ["payments/reconcile.py", "payments/payout.py"],
           comments=[{"author": "priya", "created_at": _iso(when + timedelta(hours=2)),
                      "body": "Heads up: ask Ravi at Razorpay about settlement file delays."}] if i == 1 else None)

    for i in range(3):
        when = base + timedelta(days=60 + i * 21)
        commit(LEAVER, when, f"webhooks: dispatcher improvement {i + 1}", ["webhooks/dispatcher.py"])
        pr(60 + i, LEAVER, when, f"webhooks: dispatcher improvement {i + 1}", "Dispatcher update.", ["priya"],
           ["webhooks/dispatcher.py"])

    for i in range(2):
        when = base + timedelta(days=40 + i * 30)
        commit(LEAVER, when, f"ledger: settlement batch {i + 1}", ["ledger/settle.py", "ledger/models.py"])
        commit("karan", when + timedelta(hours=6), f"ledger: model migration {i + 1}", ["ledger/models.py"])
        body = "Decided to keep Postgres as the source of truth for the ledger; settlement runs nightly." if i == 0 else "Batch tweak."
        pr(70 + i, LEAVER, when, f"ledger: settlement batch {i + 1}", body, ["karan"], ["ledger/settle.py", "ledger/models.py"])

    commit(LEAVER, datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc), "webhooks: fan out events via Redis streams",
           ["webhooks/dispatcher.py"])
    commit("karan", datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc), "infra: wire up Sentry for the payments service",
           ["infra/sentry.py"])
    commit(LEAVER, datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc),
           "webhooks: add Kafka producer behind WEBHOOK_BUS flag", ["webhooks/kafka_producer.py"])

    # branches
    d = datetime(2026, 9, 2, 16, 0, tzinfo=timezone.utc)
    commit(LEAVER, d - timedelta(days=6), "refunds: partial refund model (WIP)", ["payments/refunds.py"], "feature/refund-partial")
    commit(LEAVER, d - timedelta(days=3), "refunds: split refund across settlement files", ["payments/refunds.py"], "feature/refund-partial")
    s1 = commit(LEAVER, d, "refunds: partial refund ledger entries (WIP)", ["payments/refunds.py", "ledger/settle.py"], "feature/refund-partial")
    branches.append({"name": "feature/refund-partial", "author": LEAVER, "merged": False, "ahead_by": 6,
                     "last_commit_sha": s1, "last_commit_date": _iso(d)})
    d = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    s2 = commit(LEAVER, d, "spike: idempotency key table for payouts", ["payments/idempotency.py"], "spike/idempotency-keys")
    branches.append({"name": "spike/idempotency-keys", "author": LEAVER, "merged": False, "ahead_by": 2,
                     "last_commit_sha": s2, "last_commit_date": _iso(d)})
    d = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)
    s3 = commit(LEAVER, d, "webhooks: add Kafka consumer skeleton (not wired)", ["webhooks/kafka_consumer.py"], "feature/kafka-consumer")
    branches.append({"name": "feature/kafka-consumer", "author": LEAVER, "merged": False, "ahead_by": 4,
                     "last_commit_sha": s3, "last_commit_date": _iso(d)})
    branches.append({"name": "main", "author": LEAVER, "merged": True, "ahead_by": 0,
                     "last_commit_sha": commits[0]["sha"], "last_commit_date": commits[0]["date"]})

    pr(50, LEAVER, datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc), "webhooks: Kafka producer behind WEBHOOK_BUS flag",
       "Adds the producer. Consumer still lives on the feature branch. Blocked on infra topic quotas from Karan.",
       [], ["webhooks/kafka_producer.py"], state="open", merged=False, branch="feature/kafka-consumer")

    github = {"repo": "payflow-core", "commits": commits, "pull_requests": prs, "branches": branches}

    msgs = [
        ("2026-08-18T09:12:00Z", "priya", "Redis streams or Kafka for the webhook fan-out? Need to pick this week."),
        ("2026-08-18T09:30:00Z", "arjun", "Decided: we'll use Redis streams for webhook events for now."),
        ("2026-08-19T08:05:00Z", "karan", "Fine by me, Redis is already in the cluster."),
        ("2026-09-03T14:20:00Z", "arjun", "Razorpay sandbox is flaky for settlement callbacks, blocked on their team to fix it. "
                                         "Ask Ravi at Razorpay about the settlement file delays."),
        ("2026-09-10T11:00:00Z", "arjun", f"Temp creds for the staging bucket: {FAKE_AWS} , please delete after Friday."),
        ("2026-10-02T10:00:00Z", "meera", "Reminder: the reconcile job must finish before 6am IST or finance gets partial data."),
        ("2026-10-16T13:45:00Z", "karan", "Kafka topics are provisioned in staging only. Prod quota request still pending."),
        ("2026-10-20T10:10:00Z", "priya", "Are we still on Redis for webhooks? The dashboards still show Redis lag."),
        ("2026-10-26T17:30:00Z", "arjun", "Handing over next week. Careful with the refund branch, the ledger entries are not idempotent yet."),
    ]
    slack = {"channel": "#payflow-dev", "messages": [{"ts": t, "user": u, "text": x} for t, u, x in msgs]}

    rows = [
        ["PAY-142", "Migrate webhook event bus from Redis to Kafka", "In Progress", "arjun", "meera", "2026-10-12", "2026-10-28",
         "Decided in arch review to move to Kafka for replay support.", "karan: topic quotas requested"],
        ["PAY-151", "Reconciliation mismatch for partial refunds", "In Progress", "arjun", "meera", "2026-09-05", "2026-10-20",
         "Totals drift when a refund is split across two settlement files. Workaround: rerun reconcile manually with --force.", ""],
        ["PAY-160", "Rotate webhook signing secret", "To Do", "arjun", "karan", "2026-10-01", "2026-10-10",
         "Secret was shared in chat once.", f"password={FAKE_PW} was used for the old sandbox login"],
        ["PAY-138", "Add idempotency keys to payout API", "Done", "arjun", "meera", "2026-07-01", "2026-07-28", "Shipped in v2.", ""],
        ["PAY-170", "Document the reconcile cron", "To Do", "arjun", "meera", "2026-10-22", "2026-10-22", "Nobody has written this down.", ""],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Issue key", "Summary", "Status", "Assignee", "Reporter", "Created", "Updated", "Description", "Comments"])
    w.writerows(rows)
    return github, slack, buf.getvalue()


def demo_files():
    g, s, j = build_demo()
    return {"github.json": json.dumps(g, indent=2), "slack.json": json.dumps(s, indent=2), "jira.csv": j}
