"""Regression tests for the grader, run against the toy airline inside this process.

Each test here reproduces a bug found in the Oct 29 2026 review, so it cannot come back.
"""

import asyncio
import json
import random
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples import toy_airline as toy  # noqa: E402
from flightcontrol import catalog, grading, workload  # noqa: E402
from flightcontrol.client import TeamClient  # noqa: E402
from flightcontrol.workload import (Context, Demand, Ledger, Mix, Recorder,  # noqa: E402
                                    traveler)


@pytest.fixture(autouse=True)
def short_holds(monkeypatch):
    """Holds last one second so a whole audit fits in a few seconds."""
    monkeypatch.setattr(toy, "HOLD_SECONDS", 1)
    monkeypatch.setattr(workload, "HOLD_TTL_S", 1.0)
    monkeypatch.setattr(grading, "HOLD_TTL_S", 1.0)
    monkeypatch.setattr(grading, "DRAIN_EXTRA_S", 1.0)


def client_for(app) -> TeamClient:
    return TeamClient("http://airline", toy.API_KEY, transport=httpx.ASGITransport(app=app), timeout=5)


def answer(inner, method: str, path: str, payload, status: int = 200):
    """The toy airline, except one endpoint gives a fixed answer."""
    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == method and scope["path"] == path:
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
            return
        await inner(scope, receive, send)
    return app


def audit(app, **kw) -> dict:
    async def go():
        c = client_for(app)
        try:
            cat = catalog.get()
            ctx = Context(c, cat, Ledger(cat), Recorder(), random.Random(7), lambda *a, **k: None,
                          checks=True, run_tag="pytest")
            return await grading.test_audit(ctx, Demand(cat), lambda: False, **kw)
        finally:
            await c.close()
    return asyncio.run(go())


def test_toy_airline_passes_a_short_audit():
    res = audit(toy.app, duration=4, rate=15, min_bookings=5)
    assert res["gates_ok"], res["gates"]
    assert res["score"] == 1.0


def test_airline_that_finds_nothing_fails_the_audit():
    # Before the fix this scored 1.0: nothing was booked, so nothing could mismatch.
    res = audit(answer(toy.app, "GET", "/search", {"itineraries": []}), duration=4, rate=15, min_bookings=5)
    assert not res["gates_ok"]
    assert res["score"] == 0.0
    assert any("travelers were correct" in g or "bookings were made" in g for g in res["gates"])


def test_malformed_hold_answer_counts_as_a_failed_traveler():
    # Before the fix a hold_id that was not a string crashed the traveler, and it vanished.
    app = answer(toy.app, "POST", "/holds", {"hold_id": [], "total_cents": 1}, status=201)

    async def go():
        c = client_for(app)
        try:
            cat = catalog.get()
            rec = Recorder()
            ctx = Context(c, cat, Ledger(cat), rec, random.Random(3), lambda *a, **k: None,
                          checks=False, run_tag="pytest")
            demand = Demand(cat)
            rng = random.Random(5)
            results = [await traveler(ctx, demand.request(rng, date(2027, 4, 2), 1), Mix(p_hold=1.0))
                       for _ in range(12)]
            return rec, results
        finally:
            await c.close()

    rec, results = asyncio.run(go())
    assert len(rec.travelers) == 12          # nobody vanished
    held = [r for r in results if not r.ok]
    assert held, "travelers that reached the hold must fail"
    assert rec.violations["bad hold answer"] == len(held)


def test_one_travelers_problem_does_not_fail_another():
    rec = Recorder()

    async def bad():
        mine = []
        workload._traveler_problems.set(mine)
        await asyncio.sleep(0.01)
        rec.violation("invalid itinerary", "x")
        return mine

    async def good():
        mine = []
        workload._traveler_problems.set(mine)
        await asyncio.sleep(0.03)
        return mine

    async def both():
        return await asyncio.gather(asyncio.create_task(bad()), asyncio.create_task(good()))

    bad_list, good_list = asyncio.run(both())
    assert bad_list == ["invalid itinerary"]
    assert good_list == []


def test_hold_waiting_for_an_answer_counts_as_taken():
    cat = catalog.get()
    led = Ledger(cat)
    f = cat.flights[1000]
    before = led.room(f.flight_id)
    led.pending([f.flight_id], 6, +1)
    assert led.room(f.flight_id) == before - 6
    led.pending([f.flight_id], 6, -1)
    assert led.room(f.flight_id) == before


def test_party_too_big_for_the_seats_left_is_flagged():
    # Found in the second review: only completely full flights were being flagged.
    from flightcontrol.client import Resp
    from flightcontrol.workload import Request, validate_search
    cat = catalog.get()
    led = Ledger(cat)
    f = next(x for x in cat.flights if x.origin_local_date == date(2027, 4, 2))
    import time as _time
    rec = Recorder()
    ctx = Context(None, cat, led, rec, random.Random(1), lambda *a, **k: None, checks=True, run_tag="t")
    req = Request(f.origin, f.destination, f.origin_local_date, 2, "leisure")
    body = {"itineraries": [{"itinerary_id": f.flight_id, "flight_ids": [f.flight_id], "stops": 0,
                             "price_cents": 100, "seats_available": 2}]}
    now = _time.monotonic()
    led.sold[f.flight_id] = f.capacity - 1

    # Sold just now: the search may be stale, so this is allowed.
    led.sold_log[f.flight_id] = [(now - 1, f.capacity - 1)]
    validate_search(ctx, req, Resp(200, body, 1.0, 1.0), now, {})
    assert rec.violations["offered without enough seats"] == 0

    # Sold well before the search: offering 2 seats was wrong.
    led.sold_log[f.flight_id] = [(now - 60, f.capacity - 1)]
    validate_search(ctx, req, Resp(200, body, 1.0, 1.0), now, {})
    assert rec.violations["offered without enough seats"] == 1


def test_duplicate_booking_needs_both_copies_answered():
    # Found in the second review: one good copy hid a 500 on the other.
    calls = {"n": 0}

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/bookings":
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await toy.app(scope, receive, send)

    async def go():
        c = client_for(app)
        try:
            cat = catalog.get()
            rec = Recorder()
            ctx = Context(c, cat, Ledger(cat), rec, random.Random(3), lambda *a, **k: None,
                          checks=False, run_tag="pytest")
            demand = Demand(cat)
            rng = random.Random(11)
            mix = Mix(p_hold=1.0, p_abandon=0.0, p_decline=0.0, p_dup=1.0, p_cancel=0.0)
            return [await traveler(ctx, demand.request(rng, date(2027, 5, 6), 1), mix) for _ in range(8)]
        finally:
            await c.close()

    results = asyncio.run(go())
    dup = [r for r in results if "repeated booking" in r.reason]
    assert dup, [r.reason for r in results]
    assert not any(r.ok for r in dup)


def test_toy_repeats_its_first_answer_after_a_cancel():
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=toy.app), base_url="http://t",
                                     headers={"Authorization": f"Bearer {toy.API_KEY}"}) as c:
            f = catalog.get().flights[3000]
            h = (await c.post("/holds", json={"itinerary_id": f.flight_id, "passengers": 1})).json()
            body = {"hold_id": h["hold_id"], "passengers": [{"name": "A B"}],
                    "payment_token": "tok_ok_1", "idempotency_key": "k-cancel-replay"}
            first = (await c.post("/bookings", json=body)).json()
            await c.delete(f"/bookings/{first['booking_id']}")
            again = (await c.post("/bookings", json=body)).json()
            return first, again
    first, again = asyncio.run(go())
    assert first["status"] == "confirmed"
    assert again == first


def test_server_timing_longer_than_the_round_trip_is_ignored():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"server-timing", b"app;dur=99999")]})
        await send({"type": "http.response.body", "body": b"{}"})

    async def go():
        c = client_for(app)
        try:
            return await c.call("GET", "/health")
        finally:
            await c.close()
    r = asyncio.run(go())
    assert r.status == 200 and r.server_ms is None


def test_repeated_booking_must_get_the_same_answer_twice():
    # Found in the third review: a booking plus a 410 for the same request passed.
    calls = {"n": 0}

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/bookings":
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                await send({"type": "http.response.start", "status": 410,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "hold_expired", "detail": ""}'})
                return
        await toy.app(scope, receive, send)

    async def go():
        c = client_for(app)
        try:
            cat = catalog.get()
            rec = Recorder()
            ctx = Context(c, cat, Ledger(cat), rec, random.Random(3), lambda *a, **k: None,
                          checks=False, run_tag="pytest")
            rng = random.Random(13)
            mix = Mix(p_hold=1.0, p_abandon=0.0, p_decline=0.0, p_dup=1.0, p_cancel=0.0)
            res = [await traveler(ctx, Demand(cat).request(rng, date(2027, 5, 9), 1), mix) for _ in range(8)]
            return rec, res
        finally:
            await c.close()

    rec, results = asyncio.run(go())
    assert rec.violations["repeated booking answered differently"] >= 1
    assert not any(r.ok for r in results if "different answers" in r.reason)


def _dup_run(second_answer, decline: bool):
    """Every second copy of a booking gets a different answer than the first.

    second_answer is (status, body), or a function that turns the first copy's body into the
    second copy's body, so a test can change one field of a real answer.
    """
    calls = {"n": 0, "first": b"{}"}

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/bookings":
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                if callable(second_answer):
                    status, body = 200, second_answer(json.loads(calls["first"] or b"{}"))
                else:
                    status, body = second_answer
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps(body).encode()})
                return

            async def keep(msg):
                if msg["type"] == "http.response.body":
                    calls["first"] = msg.get("body", b"")
                await send(msg)
            await toy.app(scope, receive, keep)
            return
        await toy.app(scope, receive, send)

    async def go():
        c = client_for(app)
        try:
            cat = catalog.get()
            rec = Recorder()
            ctx = Context(c, cat, Ledger(cat), rec, random.Random(3), lambda *a, **k: None,
                          checks=False, run_tag="pytest")
            rng = random.Random(17)
            mix = Mix(p_hold=1.0, p_abandon=0.0, p_decline=1.0 if decline else 0.0, p_dup=1.0, p_cancel=0.0)
            return [await traveler(ctx, Demand(cat).request(rng, date(2027, 6, 3), 1), mix) for _ in range(6)]
        finally:
            await c.close()

    return asyncio.run(go())


def _reached(results):
    return [r for r in results if r.reason not in ("", "sold out")]


def test_decline_plus_server_error_fails_the_traveler():
    reached = _reached(_dup_run((500, {}), decline=True))
    assert reached and not any(r.ok for r in reached)


def test_same_booking_id_with_a_different_total_fails_the_traveler():
    reached = _reached(_dup_run(lambda first: {**first, "total_cents": first.get("total_cents", 0) + 1},
                                decline=False))
    assert reached and not any(r.ok for r in reached)


def test_same_booking_with_different_passengers_fails_the_traveler():
    reached = _reached(_dup_run(lambda first: {**first, "passengers": [{"name": "Someone Else"}]},
                                decline=False))
    assert reached and not any(r.ok for r in reached)


def test_two_declines_with_different_bodies_fail_the_traveler():
    reached = _reached(_dup_run((402, {"error": "payment_declined", "detail": "a different text"}),
                                decline=True))
    assert reached and not any(r.ok for r in reached)


def test_missing_result_is_not_blamed_when_seats_moved_just_before_the_search():
    import time as _time
    from flightcontrol.client import Resp
    from flightcontrol.workload import Request, validate_search
    cat = catalog.get()
    led = Ledger(cat)
    day = date(2027, 4, 2)
    f = next(x for x in cat.flights if x.origin_local_date == day)
    rec = Recorder()
    ctx = Context(None, cat, led, rec, random.Random(1), lambda *a, **k: None, checks=True, run_tag="t")
    req = Request(f.origin, f.destination, day, 1, "leisure")
    now = _time.monotonic()
    led.changed_at[f.flight_id] = now - 1        # a cancel one second before the search
    room = {x.flight_id: led.room(x.flight_id) for x in cat.by_origin_date[(f.origin, day)]}
    validate_search(ctx, req, Resp(200, {"itineraries": []}, 1.0, 1.0), now, room)
    # Other untouched nonstops on the route may be reported, but never the one whose seats just moved.
    assert not any(f.flight_id in e for e in rec.examples.get("missing nonstop", []))
    led.changed_at[f.flight_id] = now - 60       # untouched for a minute: now it is fair to report
    validate_search(ctx, req, Resp(200, {"itineraries": []}, 1.0, 1.0), now, room)
    assert any(f.flight_id in e for e in rec.examples.get("missing nonstop", []))
