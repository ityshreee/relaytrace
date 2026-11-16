import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "RELAYTRACE_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relaytrace.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, leaver TEXT, status TEXT DEFAULT 'draft',
  as_of TEXT, created_at TEXT, locked_at TEXT, dropped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, type TEXT, ref TEXT, ts TEXT,
  author TEXT, text TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, bucket TEXT, trust TEXT,
  component TEXT, attribute TEXT, value TEXT, summary TEXT, edited_text TEXT, ts TEXT,
  source_id INTEGER, quote TEXT, status TEXT DEFAULT 'pending', visibility TEXT DEFAULT 'all',
  conflict_group TEXT, suggested INTEGER DEFAULT 0, routed_to TEXT);
CREATE TABLE IF NOT EXISTS debrief(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, idx INTEGER, question TEXT,
  bucket TEXT, answer TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, ts TEXT, actor TEXT, action TEXT,
  claim_id INTEGER, summary TEXT, old TEXT, new TEXT);
CREATE TABLE IF NOT EXISTS redactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, kind TEXT, location TEXT);
CREATE TABLE IF NOT EXISTS scope(
  project_id INTEGER, component TEXT, PRIMARY KEY(project_id, component));
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, claim_id INTEGER, kind TEXT, note TEXT,
  author TEXT, status TEXT DEFAULT 'open', created TEXT, reply TEXT, replied_by TEXT, replied_at TEXT);
CREATE TABLE IF NOT EXISTS receipts(
  project_id INTEGER PRIMARY KEY, s_ack_at TEXT, s_note TEXT, m_ack_at TEXT, m_note TEXT);
"""

# Columns added after the first release; applied to older databases on start-up.
MIGRATIONS = {
    "claims": [("gap_state", "TEXT"), ("gap_owner", "TEXT"), ("gap_due", "TEXT")],
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")