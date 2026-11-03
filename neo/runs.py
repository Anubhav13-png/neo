"""Run store: everything a question produced, kept so any answer can be audited and replayed.

SQLite holds the metadata (runs, step checkpoints, LLM calls, review queue, feedback, curation queue, job queue);
<root>/<run id>/ holds the artifacts, written once (model, proof, certificates, answer, trace, attempts).
In production point this at Postgres + object storage with the same interface.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .util import json_default, new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, question TEXT, user TEXT, status TEXT, state TEXT,
  created TEXT, updated TEXT, ontology_version TEXT, snapshot TEXT, rules TEXT, llm TEXT, form TEXT,
  answer_summary TEXT, seconds REAL, needs_review INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS steps (run_id TEXT, step TEXT, status TEXT, started TEXT, finished TEXT, ms REAL,
  PRIMARY KEY (run_id, step));
CREATE TABLE IF NOT EXISTS llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, user TEXT, task TEXT,
  model TEXT, input_tokens INTEGER, output_tokens INTEGER, ms INTEGER, prompt_hash TEXT, created TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS reviews (run_id TEXT PRIMARY KEY, reasons TEXT, status TEXT, reviewer TEXT,
  decided TEXT, note TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, user TEXT, rating INTEGER,
  comment TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS curation (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, item TEXT, detail TEXT,
  run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, run_id TEXT, question TEXT, user TEXT, status TEXT,
  created TEXT, started TEXT, finished TEXT, error TEXT);
"""


class RunStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.db = os.path.join(root, "runs.db")
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ runs
    def create_run(self, question: str, user: str, ontology_version: str, snapshot: dict, rules: dict) -> str:
        run_id = new_id("run")
        os.makedirs(self.dir(run_id), exist_ok=True)
        with self._conn() as c:
            c.execute("INSERT INTO runs (id, question, user, status, state, created, updated, ontology_version, "
                      "snapshot, rules) VALUES (?, ?, ?, 'running', 'started', ?, ?, ?, ?, ?)",
                      (run_id, question, user, now_iso(), now_iso(), ontology_version, json.dumps(snapshot),
                       json.dumps(rules)))
        return run_id

    def update_run(self, run_id: str, **fields):
        fields["updated"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def run(self, run_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["snapshot"] = json.loads(d["snapshot"] or "{}")
        d["rules"] = json.loads(d["rules"] or "{}")
        return d

    def list_runs(self, limit: int = 50, user: Optional[str] = None) -> List[dict]:
        q = "SELECT id, question, user, status, form, answer_summary, created, seconds, needs_review FROM runs"
        args: tuple = ()
        if user:
            q += " WHERE user = ?"
            args = (user,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()]

    # ------------------------------------------------------------------ files (write once)
    def dir(self, run_id: str) -> str:
        return os.path.join(self.root, run_id)

    def write(self, run_id: str, name: str, content: Any, overwrite: bool = False) -> str:
        path = os.path.join(self.dir(run_id), name)
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"{path} is written once")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content, indent=1, default=json_default)
        tmp = path + ".tmp"
        open(tmp, "w").write(text)
        os.replace(tmp, path)
        return path

    def read(self, run_id: str, name: str, as_json: bool = True):
        path = os.path.join(self.dir(run_id), name)
        if not os.path.exists(path):
            return None
        return json.load(open(path)) if as_json else open(path).read()

    # ------------------------------------------------------------------ step checkpoints
    def step_done(self, run_id: str, step: str) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT status FROM steps WHERE run_id = ? AND step = ?", (run_id, step)).fetchone()
        return bool(r and r["status"] == "done")

    def save_step(self, run_id: str, step: str, output: Any, started: str, ms: float):
        self.write(run_id, f"steps/{step}.json", output, overwrite=True)
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO steps VALUES (?, ?, 'done', ?, ?, ?)", (run_id, step, started, now_iso(), ms))

    def load_step(self, run_id: str, step: str):
        return self.read(run_id, f"steps/{step}.json")

    def steps(self, run_id: str) -> List[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY started", (run_id,))]

    # ------------------------------------------------------------------ LLM usage
    def log_llm_call(self, run_id, user, task, model, tin, tout, ms, prompt_hash):
        with self._conn() as c:
            c.execute("INSERT INTO llm_calls (run_id, user, task, model, input_tokens, output_tokens, ms, prompt_hash, "
                      "created, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (run_id, user, task, model, tin, tout, ms, prompt_hash, now_iso(), dt.date.today().isoformat()))

    def llm_calls(self, run_id: str) -> List[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT task, model, input_tokens, output_tokens, ms, created FROM "
                                               "llm_calls WHERE run_id = ? ORDER BY id", (run_id,))]

    def tokens_today(self, user: str) -> int:
        with self._conn() as c:
            r = c.execute("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM llm_calls "
                          "WHERE user = ? AND day = ?", (user, dt.date.today().isoformat())).fetchone()
        return int(r["n"])

    # ------------------------------------------------------------------ review queue
    def enqueue_review(self, run_id: str, reasons: List[str]):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reviews (run_id, reasons, status, created) VALUES (?, ?, 'pending', ?)",
                      (run_id, json.dumps(reasons), now_iso()))
        self.update_run(run_id, needs_review=1)

    def reviews(self, status: Optional[str] = "pending") -> List[dict]:
        q = "SELECT r.*, runs.question, runs.user, runs.status AS run_status, runs.answer_summary FROM reviews r " \
            "JOIN runs ON runs.id = r.run_id"
        args: tuple = ()
        if status:
            q += " WHERE r.status = ?"
            args = (status,)
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY r.created", args).fetchall()]
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    def decide_review(self, run_id: str, reviewer: str, approve: bool, note: str = ""):
        with self._conn() as c:
            n = c.execute("UPDATE reviews SET status = ?, reviewer = ?, decided = ?, note = ? WHERE run_id = ? AND "
                          "status = 'pending'", ("approved" if approve else "rejected", reviewer, now_iso(), note,
                                                 run_id)).rowcount
        if not n:
            raise KeyError(f"no pending review for {run_id}")
        self.update_run(run_id, needs_review=0 if approve else 2)

    # ------------------------------------------------------------------ feedback and curation
    def add_feedback(self, run_id: str, user: str, rating: int, comment: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO feedback (run_id, user, rating, comment, created) VALUES (?, ?, ?, ?, ?)",
                      (run_id, user, rating, comment, now_iso()))

    def curate(self, kind: str, item: str, detail: str = "", run_id: Optional[str] = None):
        with self._conn() as c:
            c.execute("INSERT INTO curation (kind, item, detail, run_id, created) VALUES (?, ?, ?, ?, ?)",
                      (kind, item, detail[:1000], run_id, now_iso()))

    def curation_summary(self, limit: int = 50) -> List[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT kind, item, COUNT(*) AS n, MAX(created) AS last_seen, MAX(detail) AS example FROM curation "
                "GROUP BY kind, item ORDER BY n DESC LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------------ job queue
    def submit_job(self, question: str, user: str) -> str:
        job = new_id("job")
        with self._conn() as c:
            c.execute("INSERT INTO jobs (id, question, user, status, created) VALUES (?, ?, ?, 'queued', ?)",
                      (job, question, user, now_iso()))
        return job

    def claim_job(self) -> Optional[dict]:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status = 'running', started = ? WHERE id = ?", (now_iso(), r["id"]))
            return dict(r)

    def finish_job(self, job_id: str, run_id: Optional[str], error: Optional[str] = None):
        with self._conn() as c:
            c.execute("UPDATE jobs SET status = ?, run_id = ?, finished = ?, error = ? WHERE id = ?",
                      ("failed" if error else "done", run_id, now_iso(), error, job_id))

    def job(self, job_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ metrics
    def stats(self) -> Dict[str, Any]:
        with self._conn() as c:
            by_status = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")}
            lat = c.execute("SELECT AVG(seconds) AS avg, MAX(seconds) AS max FROM runs WHERE seconds IS NOT NULL").fetchone()
            tokens = c.execute("SELECT task, SUM(input_tokens) AS tin, SUM(output_tokens) AS tout, COUNT(*) AS calls "
                               "FROM llm_calls GROUP BY task").fetchall()
            pending = c.execute("SELECT COUNT(*) AS n FROM reviews WHERE status = 'pending'").fetchone()["n"]
            steps = c.execute("SELECT step, AVG(ms) AS ms, COUNT(*) AS n FROM steps GROUP BY step").fetchall()
        return {"runs_by_status": by_status, "latency_seconds": {"avg": lat["avg"], "max": lat["max"]},
                "llm": {r["task"]: {"calls": r["calls"], "input_tokens": r["tin"], "output_tokens": r["tout"]}
                        for r in tokens},
                "reviews_pending": pending,
                "step_ms": {r["step"]: {"avg_ms": round(r["ms"] or 0, 1), "count": r["n"]} for r in steps}}
