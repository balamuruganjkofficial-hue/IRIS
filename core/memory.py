"""Persistent memory: recent turns and remembered facts.

One SQLite connection per thread (WAL mode), a bounded turn history, and an
FTS5 index over facts with a plain-Python fallback for old SQLite builds.
"""

import re
import sqlite3
import threading
from pathlib import Path

from core.config import CFG, DATA
from core.text import tokens

DB = DATA / "memory.db"
_local = threading.local()
_schema_lock = threading.Lock()
_schema_done = False
_fts_ready = False
_inserts = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS iris_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT,
    content TEXT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS iris_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT UNIQUE,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS iris_facts_fts
    USING fts5(fact, content='iris_facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS iris_facts_ai AFTER INSERT ON iris_facts BEGIN
    INSERT INTO iris_facts_fts(rowid, fact) VALUES (new.id, new.fact);
END;
CREATE TRIGGER IF NOT EXISTS iris_facts_ad AFTER DELETE ON iris_facts BEGIN
    INSERT INTO iris_facts_fts(iris_facts_fts, rowid, fact)
    VALUES ('delete', old.id, old.fact);
END;
CREATE TRIGGER IF NOT EXISTS iris_facts_au AFTER UPDATE ON iris_facts BEGIN
    INSERT INTO iris_facts_fts(iris_facts_fts, rowid, fact)
    VALUES ('delete', old.id, old.fact);
    INSERT INTO iris_facts_fts(rowid, fact) VALUES (new.id, new.fact);
END;
"""


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=3000")
        _local.conn = conn
    return conn


def _run(sql, params=(), fetch=False):
    conn = _conn()
    cur = conn.execute(sql, params)
    rows = cur.fetchall() if fetch else None
    conn.commit()
    return rows


def close():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def init(db_path=None):
    """Create the schema. Tests pass a temporary path."""
    global DB, _schema_done, _fts_ready, _inserts
    if db_path is not None:
        close()
        DB = Path(db_path)
    _schema_done = False
    _fts_ready = False
    _inserts = 0
    conn = _conn()
    _ensure_schema(conn)
    return DB


def _ensure_schema(conn):
    global _schema_done, _fts_ready
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        conn.executescript(_SCHEMA)
        try:
            conn.executescript(_FTS)
            conn.execute("INSERT INTO iris_facts_fts(iris_facts_fts) VALUES ('rebuild')")
            _fts_ready = True
        except sqlite3.Error:
            _fts_ready = False          # SQLite without FTS5: fall back to scoring
        conn.commit()
        _schema_done = True


# ------------------------------------------------------------------ turns
def add_turn(role, content):
    global _inserts
    _run("INSERT INTO iris_turns (role, content) VALUES (?, ?)", (role, content))
    _inserts += 1
    if _inserts % 25 == 0:
        prune_turns()


def prune_turns():
    keep = int(CFG["memory"].get("max_turns", 400))
    _run("DELETE FROM iris_turns WHERE id <= "
         "(SELECT id FROM iris_turns ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep,))


def recent(n):
    rows = _run("SELECT role, content FROM iris_turns ORDER BY id DESC LIMIT ?",
                (n,), fetch=True)
    msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
    while msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    return msgs


# ------------------------------------------------------------------ facts
def add_fact(fact):
    _run("INSERT OR IGNORE INTO iris_facts (fact) VALUES (?)", ((fact or "").strip(),))


def search_facts(query, k=3):
    words = [w for w in tokens(query) if len(w) > 3]
    if not words:
        return []
    if _fts_ready:
        match = " OR ".join('"%s"' % w for w in words)
        try:
            rows = _run("SELECT f.fact FROM iris_facts_fts s "
                        "JOIN iris_facts f ON f.id = s.rowid "
                        "WHERE iris_facts_fts MATCH ? ORDER BY rank LIMIT ?",
                        (match, k), fetch=True)
            if rows:
                return [r[0] for r in rows]
        except sqlite3.Error:
            pass
    rows = _run("SELECT fact FROM iris_facts ORDER BY id DESC LIMIT 500", fetch=True)
    scored = []
    wanted = set(words)
    for (fact,) in rows:
        score = len(wanted & set(tokens(fact)))
        if score:
            scored.append((score, fact))
    scored.sort(reverse=True)
    return [f for _, f in scored[:k]]


def forget_fact(text):
    conn = _conn()
    cur = conn.execute("DELETE FROM iris_facts WHERE fact LIKE ?", ("%" + (text or "") + "%",))
    conn.commit()
    return cur.rowcount


def facts():
    return [r[0] for r in _run("SELECT fact FROM iris_facts ORDER BY id", fetch=True)]


init()
