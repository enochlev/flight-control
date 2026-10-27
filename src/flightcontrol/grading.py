"""Tests 1 to 5. Each graded test resets the airline, runs, drains and audits.

A test returns a result dict with `score` (0 to 1), `gates_ok`, a one-line
`summary` and the detail the report page shows.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import timedelta
from typing import Any, Callable

from . import catalog
from .client import TeamClient
from .workload import (HOLD_TTL_S, Booked, Context, Demand, Ledger, Mix, Recorder,
                       drive, p95, window_start)

RUSH_STEPS = [5, 10, 20, 30, 40]
RUSH_STEP_S = 30
RUSH_P95_MS = 500
RUSH_OK = 0.95


class RunFailed(Exception):
    """The run could not be carried out (reset failed, server unreachable)."""


def _noop(*a: Any, **k: Any) -> None:
    pass


# -- shared steps ------------------------------------------------------------------

async def health(client: TeamClient, timeout: float = 2.0) -> dict | None:
    r = await client.call("GET", "/health", timeout=timeout)
    if r.status == 200 and isinstance(r.body, dict):
        return r.body
    return None


async def reset(client: TeamClient) -> None:
    if await health(client) is None:
        raise RunFailed("GET /health did not answer 200")
    r = await client.call("POST", "/admin/reset", timeout=30)
    if r.status == 401:
        raise RunFailed("POST /admin/reset answered 401. Check the API key on your team page.")
    if r.status != 200:
        raise RunFailed(f"POST /admin/reset answered {r.status or r.error}")


async def replay_unknown(ctx: Context) -> int:
    """Resend every booking whose outcome is unknown, with its original key."""
    led = ctx.ledger
    for key, body in list(led.unknown_books.items()):
        legs, pax, iid = led.unknown_book_meta[key]
        for _ in range(3):
            r = await ctx.client.call("POST", "/bookings", json=body, timeout=5)
            ctx.rec.op("replay", r)
            if r.status in (200, 201) and isinstance(r.body, dict) and "booking_id" in r.body:
                led.confirm(Booked(r.body["booking_id"], key, iid, legs, pax,
                                   int(r.body.get("total_cents") or 0)))
                break
            if r.status in (402, 404, 409, 410):
                led.resolve_unknown(key)
                break
            await asyncio.sleep(1)
    for bid in list(led.unknown_cancels):
        r = await ctx.client.call("DELETE", f"/bookings/{bid}", timeout=5)
        if r.status == 200:
            led.cancel(bid)
    return len(led.unknown_books) + len(led.unknown_cancels)


async def export(client: TeamClient) -> dict[str, dict] | None:
    out: dict[str, dict] = {}
    offset = 0
    while True:
        r = await client.call("GET", "/admin/bookings", params={"offset": offset, "limit": 500}, timeout=10)
        if r.status != 200 or not isinstance(r.body, dict) or not isinstance(r.body.get("bookings"), list):
            return None
        page = r.body["bookings"]
        for b in page:
            if isinstance(b, dict) and "booking_id" in b:
                out[str(b["booking_id"])] = b
        if len(page) < 500:
            return out
        offset += 500
        if offset > 200_000:
            return out


async def flight_counts(ctx: Context, limit: int = 400) -> dict[str, dict | None]:
    fids = sorted(ctx.ledger.touched)
    if len(fids) > limit:
        fids = ctx.rng.sample(fids, limit)
    sem = asyncio.Semaphore(16)

    async def one(fid: str):
        async with sem:
            r = await ctx.client.call("GET", f"/admin/flights/{fid}", timeout=5)
            return fid, (r.body if r.status == 200 and isinstance(r.body, dict) else None)

    return dict(await asyncio.gather(*(one(f) for f in fids)))


async def check_flights(ctx: Context, *, drained: bool) -> tuple[list[str], bool]:
    """Compare per-flight seats with the ledger. Returns (mismatches, oversold)."""
    out, oversold = [], False
    for fid, body in (await flight_counts(ctx)).items():
        cap = ctx.cat.by_id[fid].capacity
        if body is None:
            out.append(f"{fid}: GET /admin/flights did not answer")
            continue
        sold, held = body.get("seats_sold"), body.get("seats_held")
        if isinstance(sold, int) and sold > cap:
            oversold = True
            out.append(f"{fid}: OVERSOLD {sold} of {cap}")
        if ctx.ledger.sold[fid] > cap:
            oversold = True
        if sold != ctx.ledger.sold[fid]:
            out.append(f"{fid}: seats_sold {sold}, Flight Control counts {ctx.ledger.sold[fid]}")
        if drained and held not in (0, None):
            out.append(f"{fid}: seats_held {held} after every hold expired")
    return out, oversold


async def audit(ctx: Context) -> list[str]:
    """Full reconciliation after the drain: export, stats and per-flight seats."""
    led = ctx.ledger
    problems: list[str] = []
    if led.unknown_books or led.unknown_cancels:
        problems.append(f"{len(led.unknown_books) + len(led.unknown_cancels)} requests still unknown after retries")
    ex = await export(ctx.client)
    if ex is None:
        problems.append("GET /admin/bookings failed")
    else:
        for bid, b in led.bookings.items():
            got = ex.get(bid)
            if got is None:
                problems.append(f"booking {bid} missing from export")
            elif got.get("status") != b.status:
                problems.append(f"booking {bid} is {got.get('status')}, should be {b.status}")
            elif got.get("total_cents") != b.total:
                problems.append(f"booking {bid} total {got.get('total_cents')}, should be {b.total}")
        extra = set(ex) - set(led.bookings)
        if extra:
            problems.append(f"{len(extra)} bookings in export that Flight Control never confirmed, e.g. {sorted(extra)[:3]}")
    r = await ctx.client.call("GET", "/admin/stats", timeout=5)
    if r.status != 200 or not isinstance(r.body, dict):
        problems.append("GET /admin/stats failed")
    else:
        for k, v in led.totals().items():
            if r.body.get(k) != v:
                problems.append(f"stats {k} = {r.body.get(k)}, Flight Control counts {v}")
        for k in ("active_holds", "seats_held"):
            if r.body.get(k) not in (0, None):
                problems.append(f"stats {k} = {r.body.get(k)} after every hold expired")
    flights, _ = await check_flights(ctx, drained=True)
    problems += flights
    return problems


def _violations(rec: Recorder) -> dict:
    return {k: {"count": n, "examples": rec.examples[k]} for k, n in rec.violations.most_common()}


def _base(rec: Recorder) -> dict:
    trav = rec.travelers
    return {
        "travelers": len(trav),
        "travelers_ok": sum(t.ok for t in trav),
        "not_started": rec.missed,
        "ops": rec.table(),
        "violations": _violations(rec),
        "duration_s": round(rec.elapsed(), 1),
    }


# -- the tests -----------------------------------------------------------------------

async def test_steady(ctx: Context, demand: Demand, stop: Callable[[], bool], rate: float = 1.0) -> dict:
    """Test 1 runs until told to stop. Nothing is reset or scored."""
    start = window_start(ctx.rng, ctx.cat, 45)
    while not stop():
        await drive(ctx, demand, Mix(), rate=lambda t: rate, duration=300, start=start,
                    span_days=45, max_inflight=32, stop=stop)
        start = window_start(ctx.rng, ctx.cat, 45)
    return {"score": None, "gates_ok": None, "summary": "steady traffic stopped", **_base(ctx.rec)}


async def test_rush(ctx: Context, demand: Demand, stop: Callable[[], bool]) -> dict:
    await reset(ctx.client)
    start = window_start(ctx.rng, ctx.cat, 30)

    def rate(t: float) -> float:
        return RUSH_STEPS[min(int(t // RUSH_STEP_S), len(RUSH_STEPS) - 1)]

    def tag(t: float) -> int:
        return RUSH_STEPS[min(int(t // RUSH_STEP_S), len(RUSH_STEPS) - 1)]

    ctx.emit("phase", text="rush: stepping 5 to 40 travelers per second")
    await drive(ctx, demand, Mix(), rate=rate, duration=RUSH_STEP_S * len(RUSH_STEPS),
                start=start, span_days=30, tag=tag, stop=stop)
    if ctx.background:
        await asyncio.wait(ctx.background)
    await replay_unknown(ctx)
    flights, oversold = await check_flights(ctx, drained=False)
    steps = []
    best = 0
    for s in RUSH_STEPS:
        ts = [t for t in ctx.rec.travelers if t.tag == s]
        ok = sum(t.ok for t in ts) / len(ts) if ts else 0.0
        lat = p95([t.server_ms for t in ts if t.ok]) or 0.0
        passed = bool(ts) and ok >= RUSH_OK and lat <= RUSH_P95_MS
        steps.append({"rate": s, "travelers": len(ts), "correct": round(ok, 3), "p95_server_ms": lat, "passed": passed})
        if passed:
            best = s
    partial = [f for f in flights if "seats_sold" in f]
    gates = []
    if oversold:
        gates.append("a flight was oversold")
    if partial:
        gates.append("seat counts disagree with confirmed bookings (partial itinerary or lost write)")
    score = 0.0 if gates else best / RUSH_STEPS[-1]
    return {"score": score, "gates_ok": not gates, "gates": gates, "steps": steps,
            "flight_mismatches": flights[:30],
            "summary": f"highest passing step {best}/s" + (f", gate failed: {gates[0]}" if gates else ""),
            **_base(ctx.rec)}


async def _book_seats(ctx: Context, fid: str, n: int) -> bool:
    """Fill a flight with n seats using 9-passenger bookings."""
    while n > 0:
        k = min(9, n)
        r = await ctx.client.call("POST", "/holds", json={"itinerary_id": fid, "passengers": k}, timeout=5)
        ctx.rec.op("fill hold", r)
        if r.status != 201 or not isinstance(r.body, dict):
            return False
        body = {"hold_id": r.body["hold_id"], "passengers": ctx.names(k),
                "payment_token": ctx.token(False), "idempotency_key": ctx.key()}
        total = r.body.get("total_cents", 0)
        b = await ctx.client.call("POST", "/bookings", json=body, timeout=5)
        ctx.rec.op("fill book", b)
        if b.status not in (200, 201) or not isinstance(b.body, dict):
            return False
        ctx.ledger.confirm(Booked(b.body["booking_id"], body["idempotency_key"], fid, [fid], k, int(total)))
        n -= k
    return True


async def test_last_seat(ctx: Context, demand: Demand, stop: Callable[[], bool],
                         rounds: int = 10, contenders: int = 40) -> dict:
    await reset(ctx.client)
    start = window_start(ctx.rng, ctx.cat, 30)
    end = start + timedelta(days=30)
    pool = [f for f in ctx.cat.flights if start <= f.origin_local_date < end]
    results, oversold = [], False
    for i in range(rounds):
        if stop():
            break
        f = ctx.rng.choice(pool)
        left = ctx.rng.randint(1, 3)
        ctx.emit("phase", text=f"last seat round {i + 1}: {f.origin}-{f.destination}, {left} left")
        if not await _book_seats(ctx, f.flight_id, f.capacity - left):
            results.append({"flight": f.flight_id, "left": left, "passed": False, "why": "could not fill the flight"})
            continue

        async def contender() -> tuple[str, Any]:
            r = await ctx.client.call("POST", "/holds", json={"itinerary_id": f.flight_id, "passengers": 1}, timeout=5)
            ctx.rec.op("rush hold", r)
            if r.status == 409:
                return "409", None
            if r.status != 201 or not isinstance(r.body, dict):
                return f"hold {r.status or r.error}", None
            body = {"hold_id": r.body["hold_id"], "passengers": ctx.names(1),
                    "payment_token": ctx.token(False), "idempotency_key": ctx.key()}
            b = await ctx.client.call("POST", "/bookings", json=body, timeout=5)
            ctx.rec.op("rush book", b)
            if b.status in (200, 201) and isinstance(b.body, dict) and "booking_id" in b.body:
                ctx.ledger.confirm(Booked(b.body["booking_id"], body["idempotency_key"], f.flight_id,
                                          [f.flight_id], 1, int(r.body.get("total_cents", 0))))
                return "booked", b.body["booking_id"]
            return f"book {b.status or b.error}", None

        outs = await asyncio.gather(*(contender() for _ in range(contenders)))
        booked = sum(o[0] == "booked" for o in outs)
        losers_ok = all(o[0] in ("booked", "409") for o in outs)
        r = await ctx.client.call("GET", f"/admin/flights/{f.flight_id}", timeout=5)
        sold = r.body.get("seats_sold") if isinstance(r.body, dict) else None
        if booked > left or (isinstance(sold, int) and sold > f.capacity):
            oversold = True
        passed = booked == left and losers_ok and sold == f.capacity
        why = "" if passed else (f"{booked} booked for {left} seats" if booked != left else
                                 "a loser got something other than 409" if not losers_ok else
                                 f"admin says {sold} sold of {f.capacity}")
        results.append({"flight": f.flight_id, "left": left, "booked": booked, "seats_sold": sold,
                        "outcomes": {k: sum(o[0] == k for o in outs) for k in {o[0] for o in outs}},
                        "passed": passed, "why": why})
    passed = sum(r["passed"] for r in results)
    gates = ["a flight was oversold"] if oversold else []
    score = 0.0 if gates else passed / rounds
    return {"score": score, "gates_ok": not gates, "gates": gates, "rounds": results,
            "summary": f"{passed} of {rounds} rounds exact" + (", OVERSOLD" if oversold else ""),
            **_base(ctx.rec)}


async def test_restart(ctx: Context, demand: Demand, stop: Callable[[], bool],
                       duration: float = 180, restart_at: float = 60) -> dict:
    await reset(ctx.client)
    h = await health(ctx.client)
    boot_before = (h or {}).get("boot_id")
    start = window_start(ctx.rng, ctx.cat, 30)
    timeline: dict[str, Any] = {"boot_before": boot_before}
    t0 = time.monotonic()

    async def restarter() -> None:
        await asyncio.sleep(restart_at)
        ctx.emit("phase", text="calling POST /admin/restart")
        r = await ctx.client.call("POST", "/admin/restart", timeout=1.5)
        timeline["restart_status"] = r.status or r.error
        timeline["restart_at"] = round(time.monotonic() - t0, 1)
        while time.monotonic() - t0 < restart_at + 90:
            hb = await health(ctx.client, timeout=1.0)
            now = round(time.monotonic() - t0, 1)
            if hb is None and "down_at" not in timeline:
                timeline["down_at"] = now
            if hb is not None and hb.get("boot_id") != boot_before:
                timeline["up_at"] = now
                timeline["boot_after"] = hb.get("boot_id")
                ctx.emit("phase", text=f"back up after {now - restart_at:.0f} s with a new boot_id")
                return
            await asyncio.sleep(1)

    task = asyncio.create_task(restarter())
    await drive(ctx, demand, Mix(p_cancel=0.05), rate=lambda t: 5, duration=duration,
                start=start, span_days=30, stop=stop)
    await task
    if ctx.background:
        await asyncio.wait(ctx.background)
    await replay_unknown(ctx)
    lost = []
    sem = asyncio.Semaphore(16)

    async def look(b: Booked):
        async with sem:
            r = await ctx.client.call("GET", f"/bookings/{b.booking_id}", timeout=5)
            if r.status != 200 or not isinstance(r.body, dict) or r.body.get("status") != b.status:
                lost.append(f"{b.booking_id} ({r.status or r.error})")

    await asyncio.gather(*(look(b) for b in ctx.ledger.bookings.values()))
    gates = []
    if timeline.get("restart_status") != 202:
        gates.append(f"POST /admin/restart answered {timeline.get('restart_status')}, expected 202")
    if "up_at" not in timeline:
        gates.append("never came back with a new boot_id")
    elif timeline["up_at"] - restart_at > 60:
        gates.append(f"took {timeline['up_at'] - restart_at:.0f} s to come back")
    if lost:
        gates.append(f"{len(lost)} confirmed bookings missing after the restart")
    up = timeline.get("up_at", duration)
    after = [t for t in ctx.rec.travelers if t.started >= up + 5]
    frac = sum(t.ok for t in after) / len(after) if after else 0.0
    if "down_at" in timeline and "up_at" in timeline:
        timeline["down_s"] = round(timeline["up_at"] - timeline["down_at"], 1)
    score = 0.0 if gates else frac
    return {"score": score, "gates_ok": not gates, "gates": gates, "timeline": timeline,
            "lost": lost[:30], "after_recovery": {"travelers": len(after), "correct": round(frac, 3)},
            "summary": (f"back in {timeline['up_at'] - restart_at:.0f} s, {frac:.0%} correct after"
                        if "up_at" in timeline else "did not come back") + (f", {gates[0]}" if gates else ""),
            **_base(ctx.rec)}


async def test_audit(ctx: Context, demand: Demand, stop: Callable[[], bool],
                     duration: float = 180, rate: float = 10) -> dict:
    await reset(ctx.client)
    start = window_start(ctx.rng, ctx.cat, 20)
    mix = Mix(p_hold=0.5, p_abandon=0.25, p_decline=0.1, p_dup=0.15, p_cancel=0.15,
              cancel_delay=(1.0, 30.0))
    ctx.emit("phase", text="audit: mixed traffic with duplicates, cancels and abandoned holds")
    await drive(ctx, demand, mix, rate=lambda t: rate, duration=duration, start=start,
                span_days=20, stop=stop)
    if ctx.background:
        await asyncio.wait(ctx.background)
    ctx.emit("phase", text=f"draining {HOLD_TTL_S + 10:.0f} s so every hold expires")
    await asyncio.sleep(HOLD_TTL_S + 10)
    await replay_unknown(ctx)
    problems = await audit(ctx)
    oversold = any("OVERSOLD" in p for p in problems)
    gates = problems[:]
    score = 0.0 if gates else 1.0
    return {"score": score, "gates_ok": not gates, "gates": gates[:5], "mismatches": problems[:60],
            "ledger": ctx.ledger.totals(),
            "summary": "everything matches" if not gates else
                       f"{len(problems)} mismatches" + (", OVERSOLD" if oversold else ""),
            **_base(ctx.rec)}


TESTS = {1: test_steady, 2: test_rush, 3: test_last_seat, 4: test_restart, 5: test_audit}


async def run_test(test: int, client: TeamClient, *, seed: int, run_tag: str,
                   emit: Callable[..., None] = _noop, stop: Callable[[], bool] = lambda: False,
                   rec: Recorder | None = None, steady_rate: float = 1.0) -> dict:
    cat = catalog.get()
    rng = random.Random(seed)
    ctx = Context(client, cat, Ledger(cat), rec or Recorder(), rng, emit,
                  checks=test != 1, run_tag=run_tag)
    demand = Demand(cat)
    try:
        if test == 1:
            return await test_steady(ctx, demand, stop, rate=steady_rate)
        return await TESTS[test](ctx, demand, stop)
    except RunFailed as e:
        return {"score": 0.0, "gates_ok": False, "gates": [str(e)], "summary": str(e),
                "failed": True, **_base(ctx.rec)}
