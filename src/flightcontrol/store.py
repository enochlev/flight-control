"""Flight Control's own state, in one SQLite file shared by the web and engine processes.

The web process writes teams and queued runs. The engine process claims runs,
writes their results and publishes the live snapshot. Both use WAL mode with a
busy timeout, and every write is a short transaction.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    approved_url TEXT NOT NULL DEFAULT '',
    steady INTEGER NOT NULL DEFAULT 0,
    is_house INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    test INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    official INTEGER NOT NULL DEFAULT 0,
    seed INTEGER NOT NULL,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    score REAL,
    gates_ok INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS runs_team ON runs(team_id, test, id);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

TEST_NAMES = {1: "Steady", 2: "Rush", 3: "Last seat", 4: "Restart", 5: "Audit"}
WEIGHTS = {2: 40, 3: 20, 4: 20, 5: 20}
PRACTICE_RUNS_PER_DAY = 12


def db_path() -> Path:
    return Path(os.environ.get("FC_DB", "data/flightcontrol.sqlite3"))


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        yield con
    finally:
        con.close()


def init(path: Path | None = None) -> None:
    with connect(path) as con:
        con.executescript(SCHEMA)


# -- teams -------------------------------------------------------------------

def create_team(name: str, slug: str, is_house: bool = False, url: str = "",
                api_key: str = "") -> dict:
    token = secrets.token_urlsafe(18)
    with connect() as con:
        con.execute(
            "INSERT INTO teams (slug, name, token, url, api_key, approved_url, is_house, created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (slug, name, token, url, api_key, url if is_house else "", int(is_house), time.time()))
    return team_by_slug(slug)  # type: ignore[return-value]


def _team(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def team_by_slug(slug: str) -> dict | None:
    with connect() as con:
        return _team(con.execute("SELECT * FROM teams WHERE slug=?", (slug,)).fetchone())


def team_by_token(token: str) -> dict | None:
    with connect() as con:
        return _team(con.execute("SELECT * FROM teams WHERE token=?", (token,)).fetchone())


def team_by_id(team_id: int) -> dict | None:
    with connect() as con:
        return _team(con.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone())


def teams() -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM teams ORDER BY is_house DESC, name")]


def update_team(team_id: int, **fields: Any) -> None:
    allowed = {"url", "api_key", "approved_url", "steady", "name"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    with connect() as con:
        con.execute(f"UPDATE teams SET {cols} WHERE id=?", (*sets.values(), team_id))


def url_ready(team: dict) -> bool:
    """A team URL can be called only once the instructor approved that exact URL."""
    return bool(team["url"]) and team["url"] == team["approved_url"] and bool(team["api_key"])


# -- runs --------------------------------------------------------------------

def queue_run(team_id: int, test: int, official: bool = False, seed: int | None = None) -> int:
    seed = seed if seed is not None else secrets.randbelow(2**31)
    with connect() as con:
        cur = con.execute(
            "INSERT INTO runs (team_id, test, official, seed, created) VALUES (?,?,?,?,?)",
            (team_id, test, int(official), seed, time.time()))
        return int(cur.lastrowid)  # type: ignore[arg-type]


def practice_runs_today(team_id: int) -> int:
    since = time.time() - 86400
    with connect() as con:
        return con.execute(
            "SELECT COUNT(*) FROM runs WHERE team_id=? AND official=0 AND created>?",
            (team_id, since)).fetchone()[0]


def active_run(team_id: int) -> dict | None:
    with connect() as con:
        r = con.execute(
            "SELECT * FROM runs WHERE team_id=? AND status IN ('queued','running') ORDER BY id LIMIT 1",
            (team_id,)).fetchone()
        return dict(r) if r else None


def claim_next_run(busy_team_ids: set[int]) -> dict | None:
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT * FROM runs WHERE status='queued' ORDER BY id").fetchall()
        for r in rows:
            if r["team_id"] in busy_team_ids:
                continue
            con.execute("UPDATE runs SET status='running', started=? WHERE id=?", (time.time(), r["id"]))
            con.execute("COMMIT")
            d = dict(r)
            d["status"] = "running"
            return d
        con.execute("COMMIT")
        return None


def finish_run(run_id: int, status: str, score: float | None, gates_ok: bool | None,
               summary: str, result: dict) -> None:
    with connect() as con:
        con.execute(
            "UPDATE runs SET status=?, finished=?, score=?, gates_ok=?, summary=?, result=? WHERE id=?",
            (status, time.time(), score, None if gates_ok is None else int(gates_ok), summary,
             json.dumps(result, default=str), run_id))


def abandon_running_runs() -> None:
    """A run interrupted by an engine restart is void, never resumed."""
    with connect() as con:
        con.execute(
            "UPDATE runs SET status='void', finished=?, summary='engine restarted during the run'"
            " WHERE status='running'", (time.time(),))


def cancel_run(run_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE runs SET status='cancelled', finished=? WHERE id=? AND status='queued'",
                    (time.time(), run_id))


def run(run_id: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"] or "{}")
        return d


def runs_for(team_id: int | None = None, limit: int = 50) -> list[dict]:
    q = ("SELECT runs.*, teams.name AS team_name, teams.slug AS team_slug FROM runs"
         " JOIN teams ON teams.id = runs.team_id")
    args: tuple = ()
    if team_id is not None:
        q += " WHERE team_id=?"
        args = (team_id,)
    q += " ORDER BY runs.id DESC LIMIT ?"
    with connect() as con:
        return [dict(r) for r in con.execute(q, (*args, limit))]


def leaderboard() -> list[dict]:
    """Best of each team's last three finished runs per scored test."""
    out = []
    with connect() as con:
        for t in con.execute("SELECT * FROM teams ORDER BY name"):
            row = {"team": t["name"], "slug": t["slug"], "is_house": bool(t["is_house"]), "tests": {}}
            total = 0.0
            for test, w in WEIGHTS.items():
                rs = con.execute(
                    "SELECT score FROM runs WHERE team_id=? AND test=? AND status='done'"
                    " ORDER BY id DESC LIMIT 3", (t["id"], test)).fetchall()
                best = max((r["score"] or 0.0 for r in rs), default=None)
                row["tests"][test] = best
                total += w * (best or 0.0)
            row["total"] = round(total, 1)
            row["has_runs"] = any(v is not None for v in row["tests"].values())
            out.append(row)
    out.sort(key=lambda r: -r["total"])
    return out


# -- live snapshot -------------------------------------------------------------

def put(key: str, value: Any) -> None:
    with connect() as con:
        con.execute("INSERT INTO kv (key, value, updated) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                    (key, json.dumps(value, default=str), time.time()))


def get(key: str, default: Any = None) -> tuple[Any, float]:
    with connect() as con:
        r = con.execute("SELECT value, updated FROM kv WHERE key=?", (key,)).fetchone()
    if not r:
        return default, 0.0
    return json.loads(r["value"]), r["updated"]
