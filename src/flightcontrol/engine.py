"""The traffic engine: one process that sends every request Flight Control makes.

It never serves web pages. Each second it reconciles what should be running
(steady traffic for connected teams, queued test runs) with what is running,
and publishes a live snapshot to SQLite for the web process to read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import catalog, store
from .client import TeamClient, UnsafeURL, check_url
from .grading import run_test
from .workload import Recorder, p95

log = logging.getLogger("flightcontrol.engine")
MAX_PARALLEL_RUNS = int(os.environ.get("FC_MAX_PARALLEL_RUNS", "2"))
STEADY_RATE = float(os.environ.get("FC_STEADY_RATE", "1"))
HOUSE_RATE = float(os.environ.get("FC_HOUSE_RATE", "2"))
DEV = os.environ.get("FC_DEV") == "1"


@dataclass
class TeamState:
    slug: str
    name: str
    is_house: bool
    steady_task: asyncio.Task | None = None
    steady_stop: bool = False
    steady_rec: Recorder | None = None
    run_task: asyncio.Task | None = None
    run_id: int | None = None
    run_test: int | None = None
    run_rec: Recorder | None = None
    run_started: float = 0.0
    phase: str = ""
    error: str = ""
    bookings: int = 0
    extra: dict = field(default_factory=dict)
    calls: deque = field(default_factory=lambda: deque(maxlen=100))


class Engine:
    def __init__(self) -> None:
        self.state: dict[int, TeamState] = {}
        self.events: deque[dict] = deque(maxlen=400)
        self.started = time.time()

    # -- events --------------------------------------------------------------------

    def emitter(self, ts: TeamState, test: int):
        def emit(kind: str, **data: Any) -> None:
            if kind == "book":
                ts.bookings += 1
            if kind == "phase":
                ts.phase = data.get("text", "")
            self.events.append({"t": time.time(), "team": ts.name, "house": ts.is_house,
                                "test": test, "kind": kind, **data})
        return emit

    # -- house airline -------------------------------------------------------------

    def ensure_house(self) -> None:
        url, key = os.environ.get("FC_HOUSE_URL", ""), os.environ.get("FC_HOUSE_KEY", "")
        if not url or not key:
            return
        t = store.team_by_slug("house")
        if t is None:
            store.create_team("House Airline", "house", is_house=True, url=url, api_key=key)
            t = store.team_by_slug("house")
        store.update_team(t["id"], url=url, approved_url=url, api_key=key)
        if not t["steady"]:
            store.update_team(t["id"], steady=1)

    def client_for(self, team: dict, ts: TeamState | None = None) -> TeamClient:
        base = check_url(team["url"], allow_private=bool(team["is_house"]) or DEV)
        on_call = None
        if ts is not None:
            def on_call(method: str, path: str, r) -> None:
                ts.calls.append({"t": time.time(), "m": method, "p": path[:120], "s": r.status,
                                 "ms": round(r.rtt_ms, 1), "e": r.error})
        return TeamClient(base, team["api_key"], on_call=on_call)

    # -- steady traffic ------------------------------------------------------------

    async def steady(self, team: dict, ts: TeamState) -> None:
        ts.steady_stop = False
        ts.steady_rec = Recorder()
        rate = HOUSE_RATE if team["is_house"] else STEADY_RATE
        while not ts.steady_stop:
            try:
                client = self.client_for(team, ts)
            except UnsafeURL as e:
                ts.error = str(e)
                await asyncio.sleep(30)
                continue
            try:
                ts.error = ""
                await run_test(1, client, seed=int(time.time()), run_tag=f"s{team['id']}",
                               emit=self.emitter(ts, 1), stop=lambda: ts.steady_stop,
                               rec=ts.steady_rec, steady_rate=rate)
            except Exception as e:  # keep steady traffic alive no matter what a team does
                ts.error = f"{type(e).__name__}: {e}"
                log.warning("steady %s: %s", team["slug"], traceback.format_exc())
                await asyncio.sleep(5)
            finally:
                await client.close()

    # -- graded runs ---------------------------------------------------------------

    async def graded(self, run: dict, team: dict, ts: TeamState) -> None:
        ts.run_id, ts.run_test, ts.run_started = run["id"], run["test"], time.time()
        ts.run_rec = Recorder()
        ts.phase = "starting"
        emit = self.emitter(ts, run["test"])
        emit("run", text=f"test {run['test']} started")
        try:
            client = self.client_for(team, ts)
        except UnsafeURL as e:
            store.finish_run(run["id"], "failed", 0.0, False, f"URL refused: {e}", {})
            ts.run_id = None
            return
        try:
            res = await run_test(run["test"], client, seed=run["seed"], run_tag=f"r{run['id']}",
                                 emit=emit, rec=ts.run_rec)
            status = "failed" if res.get("failed") else "done"
            store.finish_run(run["id"], status, res.get("score"), res.get("gates_ok"),
                             res.get("summary", ""), res)
            emit("run", text=f"test {run['test']} finished: {res.get('summary', '')}")
        except Exception as e:
            log.error("run %s: %s", run["id"], traceback.format_exc())
            store.finish_run(run["id"], "failed", 0.0, False, f"Flight Control error: {type(e).__name__}", {})
        finally:
            await client.close()
            ts.run_id = ts.run_test = None
            ts.phase = ""

    # -- the loop --------------------------------------------------------------------

    async def tick(self) -> None:
        teams = {t["id"]: t for t in store.teams()}
        for tid, t in teams.items():
            ts = self.state.setdefault(tid, TeamState(t["slug"], t["name"], bool(t["is_house"])))
            ts.name = t["name"]
            busy = ts.run_task is not None and not ts.run_task.done()
            want_steady = bool(t["steady"]) and (store.url_ready(t) or bool(t["is_house"])) and not busy
            running = ts.steady_task is not None and not ts.steady_task.done()
            if want_steady and not running:
                ts.steady_task = asyncio.create_task(self.steady(t, ts))
            elif not want_steady and running:
                ts.steady_stop = True
        active = sum(1 for s in self.state.values() if s.run_task and not s.run_task.done())
        if active < MAX_PARALLEL_RUNS:
            busy_ids = {tid for tid, s in self.state.items() if s.run_task and not s.run_task.done()}
            run = store.claim_next_run(busy_ids)
            if run:
                team = teams.get(run["team_id"])
                ts = self.state[run["team_id"]]
                ts.steady_stop = True
                if ts.steady_task and not ts.steady_task.done():
                    await asyncio.wait({ts.steady_task}, timeout=15)
                ts.run_task = asyncio.create_task(self.graded(run, team, ts))

    def snapshot(self) -> dict:
        now = time.time()
        teams = []
        for tid, s in self.state.items():
            rec = s.run_rec if s.run_id else s.steady_rec
            win = rec.window(30) if rec else {"rate": 0, "ok": 0, "bad": 0}
            lat = None
            if rec:
                cut = rec.elapsed() - 30
                lat = p95([t.server_ms for t in rec.travelers[-3000:] if t.started >= cut])
            mode = (f"test {s.run_test}" if s.run_id else
                    "steady" if s.steady_task and not s.steady_task.done() and not s.steady_stop else "idle")
            detail = {}
            if s.run_id and s.run_rec:
                detail = {"run_id": s.run_id, "elapsed": round(now - s.run_started),
                          "travelers": len(s.run_rec.travelers),
                          "ok": sum(t.ok for t in s.run_rec.travelers),
                          "violations": dict(s.run_rec.violations.most_common(8))}
            elif s.steady_rec:
                detail = {"violations": dict(s.steady_rec.violations.most_common(8)),
                          "examples": {k: v[:2] for k, v in s.steady_rec.examples.items()}}
            detail["calls"] = list(s.calls)[-40:]
            teams.append({"id": tid, "slug": s.slug, "name": s.name, "house": s.is_house, "mode": mode,
                          "phase": s.phase, "error": s.error, "rate": win["rate"], "ok": win["ok"],
                          "bad": win["bad"], "p95_ms": lat, "bookings": s.bookings, "detail": detail})
        events = list(self.events)
        books = [e for e in events if e["kind"] == "book" and now - e["t"] < 20]
        feed = [e for e in events if e["kind"] in ("book", "cancel", "phase", "run")][-60:]
        return {"now": now, "engine_started": self.started, "teams": teams, "feed": feed,
                "arcs": [[e["origin"], e["destination"], e["team"], e["house"]] for e in books][-80:],
                "bookings_per_min": sum(1 for e in events if e["kind"] == "book" and now - e["t"] < 60)}

    async def main(self) -> None:
        store.init()
        store.abandon_running_runs()
        catalog.get()
        self.ensure_house()
        log.info("engine running")
        last_pub = 0.0
        while True:
            try:
                await self.tick()
            except Exception:
                log.error("tick: %s", traceback.format_exc())
            if time.time() - last_pub >= 1.0:
                try:
                    await asyncio.to_thread(store.put, "live", self.snapshot())
                    last_pub = time.time()
                except Exception:
                    log.error("publish: %s", traceback.format_exc())
            await asyncio.sleep(0.5)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Engine().main())
