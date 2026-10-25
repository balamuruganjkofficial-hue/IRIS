import re
import sqlite3
from core.config import ROOT

DB = ROOT / "data" / "memory.db"
DB.parent.mkdir(exist_ok=True)


def _run(sql, params=(), fetch=False):
    conn = sqlite3.connect(DB)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


_run("CREATE TABLE IF NOT EXISTS iris_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, "
     "role TEXT, content TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP)")
_run("CREATE TABLE IF NOT EXISTS iris_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
     "fact TEXT UNIQUE, ts DATETIME DEFAULT CURRENT_TIMESTAMP)")


def add_turn(role, content):
    _run("INSERT INTO iris_turns (role, content) VALUES (?, ?)", (role, content))


def recent(n):
    rows = _run("SELECT role, content FROM iris_turns ORDER BY id DESC LIMIT ?",
                (n,), fetch=True)
    msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
    while msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    return msgs


def add_fact(fact):
    _run("INSERT OR IGNORE INTO iris_facts (fact) VALUES (?)", (fact.strip(),))


def search_facts(query, k=3):
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 3}
    if not words:
        return []
    rows = _run("SELECT fact FROM iris_facts ORDER BY id DESC LIMIT 500", fetch=True)
    scored = []
    for (fact,) in rows:
        score = len(words & set(re.findall(r"[a-z0-9]+", fact.lower())))
        if score:
            scored.append((score, fact))
    scored.sort(reverse=True)
    return [f for _, f in scored[:k]]


def forget_fact(text):
    conn = sqlite3.connect(DB)
    try:
        cur = conn.execute("DELETE FROM iris_facts WHERE fact LIKE ?", (f"%{text}%",))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()