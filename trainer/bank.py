"""本地账本：记录每份卷子的状态 + 题目-答案缓存（越刷越快）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    session_id   TEXT PRIMARY KEY,
    qbase_type   INTEGER,
    tag_id       INTEGER,
    level        INTEGER,
    created_at   TEXT,
    submitted_at TEXT,
    state        INTEGER,
    score        REAL,
    total        REAL,
    note         TEXT,
    payload      TEXT
);
CREATE TABLE IF NOT EXISTS qcache (
    qhash      TEXT PRIMARY KEY,
    qtype      INTEGER,
    content    TEXT,
    answer     TEXT,
    source     TEXT,
    hits       INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    kind    TEXT,
    detail  TEXT
);
"""


def qhash(qtype: int, content: str, options: list | None = None) -> str:
    raw = f"{qtype}|{content}|" + "|".join(
        str(o.get("content") or "") for o in (options or [])
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Bank:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- papers ----------
    def add_paper(self, session_id, qbase_type, tag_id, level, payload: dict | None = None):
        self.conn.execute(
            "INSERT OR REPLACE INTO papers(session_id,qbase_type,tag_id,level,created_at,state,payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                str(session_id),
                qbase_type,
                tag_id,
                level,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                0,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )
        self.conn.commit()

    def finish_paper(self, session_id, state=None, score=None, total=None, note=""):
        self.conn.execute(
            "UPDATE papers SET submitted_at=?, state=COALESCE(?,state),"
            " score=COALESCE(?,score), total=COALESCE(?,total), note=? WHERE session_id=?",
            (time.strftime("%Y-%m-%d %H:%M:%S"), state, score, total, note, str(session_id)),
        )
        self.conn.commit()

    def pending_papers(self, limit: int = 5) -> list[str]:
        cur = self.conn.execute(
            "SELECT session_id FROM papers WHERE state=0 ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [r[0] for r in cur.fetchall()]

    def recent_papers(self, limit: int = 50) -> list[dict]:
        cur = self.conn.execute(
            "SELECT session_id,qbase_type,created_at,submitted_at,state,score,total,note"
            " FROM papers ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def papers_since(self, ts: str) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM papers WHERE created_at>=?", (ts,))
        return int(cur.fetchone()[0])

    # ---------- qcache ----------
    def get_answer(self, h: str):
        cur = self.conn.execute("SELECT answer,source FROM qcache WHERE qhash=?", (h,))
        row = cur.fetchone()
        if not row:
            return None
        self.conn.execute("UPDATE qcache SET hits=hits+1 WHERE qhash=?", (h,))
        self.conn.commit()
        return row[0]

    def put_answer(self, h: str, qtype: int, content: str, answer, source: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO qcache(qhash,qtype,content,answer,source,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                h,
                qtype,
                content,
                json.dumps(answer, ensure_ascii=False),
                source,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.conn.commit()

    def stats(self) -> dict:
        c = self.conn
        papers = c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM papers WHERE state=1").fetchone()[0]
        avg = c.execute("SELECT AVG(score) FROM papers WHERE score IS NOT NULL").fetchone()[0]
        cache = c.execute("SELECT COUNT(*) FROM qcache").fetchone()[0]
        return {
            "papers": papers,
            "done": done,
            "avg_score": round(avg, 2) if avg is not None else None,
            "cached_answers": cache,
        }

    def log(self, kind: str, detail: str):
        self.conn.execute(
            "INSERT INTO events(ts,kind,detail) VALUES(?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), kind, detail[:2000]),
        )
        self.conn.commit()
