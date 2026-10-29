"""Travelers, the ledger of what Flight Control was told, and the checks on every answer."""

from __future__ import annotations

import asyncio
import contextvars
import heapq
import random
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import oracle
from .catalog import AIRPORT_BY_CODE, Catalog
from .client import Resp, TeamClient

HOLD_TTL_S = 60.0
TRAVELER_DEADLINE_S = 5.0
SLACK = 3  # seats of margin before Flight Control calls a sold-out answer wrong
STALE_S = 5.0  # search answers may be this many seconds out of date

FIRST = ["Ada", "Ben", "Cora", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivan", "June", "Kai",
         "Lena", "Milo", "Nora", "Omar", "Pia", "Quinn", "Rosa", "Sam", "Tara", "Uma",
         "Vic", "Wren", "Xavi", "Yara", "Zed"]
LAST = ["Park", "Nguyen", "Okafor", "Silva", "Kowalski", "Haddad", "Larsen", "Moreau",
        "Tanaka", "Reyes", "Bauer", "Ivers", "Quist", "Abara", "Lindqvist", "Ferreira"]


# -- what Flight Control knows ---------------------------------------------------

@dataclass
class Booked:
    booking_id: str
    key: str
    itinerary_id: str
    flight_ids: list[str]
    pax: int
    total: int
    status: str = "confirmed"
    names: list[str] | None = None


class Ledger:
    """Every booking Flight Control was told about, and what that implies per flight."""

    def __init__(self, cat: Catalog) -> None:
        self.cat = cat
        self.bookings: dict[str, Booked] = {}
        self.by_key: dict[str, str] = {}
        self.sold: Counter[str] = Counter()
        self.holds: dict[str, tuple[list[str], int, float]] = {}
        self.unknown_books: dict[str, dict] = {}       # idempotency key -> request body
        self.unknown_book_meta: dict[str, tuple[list[str], int, str]] = {}
        self.unknown_cancels: set[str] = set()
        self.touched: set[str] = set()
        self._held: Counter[str] = Counter()
        self._uncertain: Counter[str] = Counter()
        self._pending: Counter[str] = Counter()     # holds sent, no answer yet
        self._expiry: list[tuple[float, str]] = []
        self.full_since: dict[str, float] = {}      # flight -> when its sold seats reached capacity
        self.changed_at: dict[str, float] = {}      # flight -> last time any seat on it moved
        self.sold_log: dict[str, list[tuple[float, int]]] = {}  # flight -> (time, seats sold) history

    def _touch(self, legs) -> None:
        now = time.monotonic()
        for f in legs:
            self.changed_at[f] = now

    def quiet_since(self, fid: str, t: float) -> bool:
        """True if nothing about this flight's seats changed after time t."""
        return self.changed_at.get(fid, 0.0) < t

    def _expire(self) -> None:
        now = time.monotonic()
        while self._expiry and self._expiry[0][0] <= now:
            _, hid = heapq.heappop(self._expiry)
            self.drop_hold(hid)

    def held(self, fid: str) -> int:
        self._expire()
        return self._held[fid]

    def uncertain(self, fid: str) -> int:
        return self._uncertain[fid]

    def room(self, fid: str) -> int:
        """Seats Flight Control is sure are free. Anything that might be taken counts as taken."""
        f = self.cat.by_id[fid]
        return (f.capacity - self.sold[fid] - self.held(fid) - self._uncertain[fid]
                - self._pending[fid])

    def pending(self, legs: list[str], pax: int, sign: int) -> None:
        self._touch(legs)
        for f in legs:
            self._pending[f] += sign * pax

    def sold_at(self, fid: str, t: float) -> int:
        """Seats sold on a flight at monotonic time t, from the booking history."""
        n = 0
        for when, sold in self.sold_log.get(fid, ()):
            if when > t:
                break
            n = sold
        return n

    def least_sold_between(self, fid: str, t0: float, t1: float) -> int:
        """The fewest seats sold on a flight at any moment from t0 to t1."""
        least = self.sold_at(fid, t0)
        for when, sold in self.sold_log.get(fid, ()):
            if t0 < when <= t1:
                least = min(least, sold)
        return least

    def _update_full(self, fid: str) -> None:
        self.sold_log.setdefault(fid, []).append((time.monotonic(), self.sold[fid]))
        if self.sold[fid] >= self.cat.by_id[fid].capacity:
            self.full_since.setdefault(fid, time.monotonic())
        else:
            self.full_since.pop(fid, None)

    def add_hold(self, hold_id: str, legs: list[str], pax: int) -> None:
        exp = time.monotonic() + HOLD_TTL_S + 2
        self.holds[hold_id] = (legs, pax, exp)
        heapq.heappush(self._expiry, (exp, hold_id))
        self._touch(legs)
        for f in legs:
            self._held[f] += pax
        self.touched.update(legs)

    def drop_hold(self, hold_id: str) -> None:
        h = self.holds.pop(hold_id, None)
        if h:
            self._touch(h[0])
            for f in h[0]:
                self._held[f] -= h[1]

    def mark_unknown(self, key: str, body: dict, legs: list[str], pax: int, iid: str) -> None:
        self.unknown_books[key] = body
        self.unknown_book_meta[key] = (legs, pax, iid)
        for f in legs:
            self._uncertain[f] += pax

    def resolve_unknown(self, key: str) -> None:
        self.unknown_books.pop(key, None)
        meta = self.unknown_book_meta.pop(key, None)
        if meta:
            for f in meta[0]:
                self._uncertain[f] -= meta[1]

    def confirm(self, b: Booked) -> None:
        self.resolve_unknown(b.key)
        if b.booking_id in self.bookings:
            return
        self.bookings[b.booking_id] = b
        self.by_key[b.key] = b.booking_id
        self._touch(b.flight_ids)
        for f in b.flight_ids:
            self.sold[f] += b.pax
            self._update_full(f)
        self.touched.update(b.flight_ids)

    def cancel(self, booking_id: str) -> None:
        b = self.bookings.get(booking_id)
        self.unknown_cancels.discard(booking_id)
        if b and b.status == "confirmed":
            self._touch(b.flight_ids)
            b.status = "cancelled"
            for f in b.flight_ids:
                self.sold[f] -= b.pax
                self._update_full(f)

    def totals(self) -> dict[str, int]:
        conf = [b for b in self.bookings.values() if b.status == "confirmed"]
        return {
            "bookings_confirmed": len(conf),
            "bookings_cancelled": sum(b.status == "cancelled" for b in self.bookings.values()),
            "seats_sold": sum(b.pax for b in conf),
            "revenue_cents": sum(b.total for b in conf),
        }


# -- measurements ------------------------------------------------------------

def p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(0.95 * len(s)))], 1)


@dataclass
class OpStats:
    n: int = 0
    codes: Counter = field(default_factory=Counter)
    rtt: list[float] = field(default_factory=list)
    srv: list[float] = field(default_factory=list)

    def add(self, r: Resp) -> None:
        self.n += 1
        self.codes[str(r.status) if r.status else (r.error or "none").split(":")[0]] += 1
        if len(self.rtt) < 100_000:
            self.rtt.append(r.rtt_ms)
            if r.server_ms is not None:
                self.srv.append(r.server_ms)


@dataclass
class TravelerResult:
    started: float
    ok: bool
    reason: str = ""
    server_ms: float = 0.0
    tag: Any = None


# The problems found during the current traveler. Each traveler runs in its own
# task, and a task gets its own copy of this variable, so one traveler's problem
# never counts against another traveler running at the same time.
_traveler_problems: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "fc_traveler_problems", default=None)


class Recorder:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.ops: dict[str, OpStats] = defaultdict(OpStats)
        self.travelers: list[TravelerResult] = []
        self.violations: Counter[str] = Counter()
        self.examples: dict[str, list[str]] = defaultdict(list)
        self.missed = 0
        self.recent: deque[tuple[float, bool]] = deque(maxlen=5000)

    def op(self, name: str, r: Resp) -> None:
        self.ops[name].add(r)

    def violation(self, kind: str, detail: str) -> None:
        mine = _traveler_problems.get()
        if mine is not None:
            mine.append(kind)
        self.violations[kind] += 1
        if len(self.examples[kind]) < 5:
            self.examples[kind].append(detail)

    def traveler(self, res: TravelerResult) -> None:
        self.travelers.append(res)
        self.recent.append((time.monotonic(), res.ok))

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def table(self) -> dict:
        return {name: {"n": s.n, "codes": dict(s.codes), "p95_rtt_ms": p95(s.rtt),
                       "p95_server_ms": p95(s.srv)} for name, s in sorted(self.ops.items())}

    def window(self, seconds: float = 30.0) -> dict:
        cut = time.monotonic() - seconds
        xs = [ok for t, ok in self.recent if t >= cut]
        return {"rate": round(len(xs) / seconds, 2), "ok": sum(xs), "bad": len(xs) - sum(xs)}


# -- traveler requests -----------------------------------------------------------

@dataclass(frozen=True)
class Request:
    origin: str
    destination: str
    day: date
    pax: int
    kind: str          # leisure | business


class Demand:
    """City pairs with at least three itineraries on a normal day, weighted by size."""

    _pairs: list[tuple[str, str]] | None = None
    _weights: list[float] | None = None

    def __init__(self, cat: Catalog) -> None:
        self.cat = cat
        if Demand._pairs is None:
            probe = date(cat.year, 3, 17)
            pairs, weights = [], []
            for a in AIRPORT_BY_CODE:
                for b in AIRPORT_BY_CODE:
                    if a != b and len(oracle.itineraries(cat, a, b, probe)) >= 3:
                        pairs.append((a, b))
                        weights.append(AIRPORT_BY_CODE[a].weight * AIRPORT_BY_CODE[b].weight)
            Demand._pairs, Demand._weights = pairs, weights

    def request(self, rng: random.Random, start: date, span_days: int) -> Request:
        a, b = rng.choices(self._pairs, self._weights)[0]  # type: ignore[arg-type]
        day = start + timedelta(days=rng.randrange(span_days))
        pax = rng.choices([1, 2, 3, 4, 5, 6], [55, 25, 10, 5, 3, 2])[0]
        kind = rng.choices(["leisure", "business"], [70, 30])[0]
        return Request(a, b, day, pax, kind)


@dataclass
class Mix:
    p_hold: float = 0.4
    p_abandon: float = 0.15
    p_decline: float = 0.1
    p_dup: float = 0.0
    p_cancel: float = 0.08
    cancel_delay: tuple[float, float] = (2.0, 20.0)


# -- one traveler ------------------------------------------------------------------

Emit = Callable[..., None]


class Context:
    def __init__(self, client: TeamClient, cat: Catalog, ledger: Ledger, rec: Recorder,
                 rng: random.Random, emit: Emit, *, checks: bool, run_tag: str) -> None:
        self.client, self.cat, self.ledger, self.rec = client, cat, ledger, rec
        self.rng, self.emit, self.checks, self.run_tag = rng, emit, checks, run_tag
        self.background: set[asyncio.Task] = set()

    def key(self) -> str:
        return f"fc-{self.run_tag}-{uuid.uuid4().hex[:16]}"

    def names(self, n: int) -> list[dict]:
        return [{"name": f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)}"} for _ in range(n)]

    def token(self, decline: bool) -> str:
        return f"tok_{'decline' if decline else 'ok'}_{self.rng.randrange(10**6):06d}"

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.background.add(t)
        t.add_done_callback(self.background.discard)


def _bad(r: Resp) -> bool:
    return r.status == 0 or r.status >= 500


def validate_search(ctx: Context, req: Request, r: Resp, sent_at: float,
                    room_before: dict[str, int] | None) -> list[dict]:
    """Check a search answer and return its itineraries. Problems become violations.

    Search results may be a few seconds stale, so a flight only counts as wrongly
    offered if it was already full when the search was sent and still is, and a
    missing result only counts if it had room both before and after the search.
    """
    cat, led = ctx.cat, ctx.ledger
    body = r.body if isinstance(r.body, dict) else None
    its = body.get("itineraries") if body else None
    if not isinstance(its, list):
        ctx.rec.violation("bad search response", f"status {r.status}, body {str(r.body)[:120]}")
        return []
    if len(its) > oracle.MAX_RESULTS:
        ctx.rec.violation("more than 20 results", f"{len(its)} for {req.origin}-{req.destination}")
    good = []
    returned = set()
    for it in its:
        if not isinstance(it, dict) or not isinstance(it.get("itinerary_id"), str):
            ctx.rec.violation("bad itinerary shape", str(it)[:120])
            continue
        iid = it["itinerary_id"]
        why = oracle.check_itinerary(cat, iid, req.origin, req.destination, req.day, None)
        if why:
            ctx.rec.violation("invalid itinerary", f"{iid}: {why}")
            continue
        if it.get("flight_ids") != iid.split("+"):
            ctx.rec.violation("flight_ids do not match itinerary_id", iid)
        seats = it.get("seats_available")
        price = it.get("price_cents")
        if not isinstance(seats, int) or seats < req.pax:
            ctx.rec.violation("offered without enough seats", f"{iid} seats_available={seats}, passengers={req.pax}")
            continue
        if not isinstance(price, int) or price < 0:
            ctx.rec.violation("bad price", f"{iid} price_cents={price}")
            continue
        legs = iid.split("+")
        if ctx.checks:
            for fid in legs:
                cap = cat.by_id[fid].capacity
                # Wrong only if the party never fit at any moment in the allowed staleness window.
                was = led.least_sold_between(fid, sent_at - STALE_S, time.monotonic())
                if was + req.pax > cap:
                    ctx.rec.violation("offered without enough seats",
                                      f"{fid}: {was} of {cap} seats were already sold {STALE_S:.0f} s "
                                      f"before this search, and it was offered for {req.pax}")
        returned.add(iid)
        good.append(it)
    if ctx.checks and room_before is not None:
        truth = oracle.itineraries(cat, req.origin, req.destination, req.day)
        # Only judge flights whose seats did not move at all in the staleness window or while
        # the search was out, so a valid answer that is a few seconds old is never wrong.
        roomy = [i for i in truth
                 if all(led.quiet_since(f.flight_id, sent_at - STALE_S)
                        and min(room_before.get(f.flight_id, 0), led.room(f.flight_id)) >= req.pax + SLACK
                        for f in i.legs)]
        for i in roomy:
            if i.stops == 0 and i.itinerary_id not in returned:
                ctx.rec.violation("missing nonstop", f"{i.itinerary_id} has room but was not returned")
        if len(roomy) >= 3 and len(returned) < 3:
            ctx.rec.violation("fewer than 3 results", f"{req.origin}-{req.destination} {req.day}: got {len(returned)}")
    return good


# What an hour and a connection are worth to a traveler, in dollars.
VALUE_OF_HOUR = {"leisure": 15.0, "business": 75.0}
CONNECTION_COST = {"leisure": 60.0, "business": 150.0}


def trip_cost(cat: Catalog, it: dict, kind: str) -> float:
    """Price per passenger plus the traveler's cost of time and connections, in dollars."""
    legs = [cat.by_id[f] for f in it["itinerary_id"].split("+")]
    hours = (legs[-1].arrival - legs[0].departure).total_seconds() / 3600
    return it["price_cents"] / 100 + VALUE_OF_HOUR[kind] * hours + CONNECTION_COST[kind] * (len(legs) - 1)


def choose(cat: Catalog, its: list[dict], req: "Request") -> dict:
    """The itinerary with the lowest total cost to this traveler. A long or
    connecting trip wins only when it is cheap enough to make up for the time."""
    return min(its, key=lambda i: (trip_cost(cat, i, req.kind), i["itinerary_id"]))


async def traveler(ctx: Context, req: Request, mix: Mix, tag: Any = None) -> TravelerResult:
    """Run one traveler and record whether every answer it got was correct.

    A traveler is correct when every call got an answer (no timeout, no 5xx),
    no rule in the contract was broken, and the whole trip took under 5 seconds.
    """
    mine: list[str] = []
    _traveler_problems.set(mine)
    t0 = time.monotonic()
    started = ctx.rec.elapsed()
    timing = [0.0]
    try:
        ok, why = await _trip(ctx, req, mix, timing)
    except Exception as e:  # an answer Flight Control could not use counts against the airline
        ctx.rec.violation("unreadable answer", f"{type(e).__name__}: {e}"[:200])
        ok, why = False, "Flight Control could not read an answer"
    late = time.monotonic() - t0 > TRAVELER_DEADLINE_S
    good = ok and not late and not mine
    reason = why if not ok else ("took longer than 5 s" if late else (mine[0] if mine else why))
    res = TravelerResult(started, good, reason, timing[0], tag)
    ctx.rec.traveler(res)
    return res


def _spent(r: Resp) -> float:
    return r.server_ms if r.server_ms is not None else r.rtt_ms


async def _trip(ctx: Context, req: Request, mix: Mix, timing: list[float]) -> tuple[bool, str]:
    led = ctx.ledger
    room_before = None
    if ctx.checks:
        truth = oracle.itineraries(ctx.cat, req.origin, req.destination, req.day)
        room_before = {f.flight_id: led.room(f.flight_id) for i in truth for f in i.legs}
    sent_at = time.monotonic()
    r = await ctx.client.call("GET", "/search", params={
        "origin": req.origin, "destination": req.destination,
        "date": req.day.isoformat(), "passengers": req.pax})
    ctx.rec.op("search", r)
    timing[0] += _spent(r)
    if r.status != 200:
        if r.status and r.status < 500:
            ctx.rec.violation("search rejected", f"{r.status} for {req.origin}-{req.destination} {req.day}")
        return False, f"search answered {r.status or r.error}"
    its = validate_search(ctx, req, r, sent_at, room_before)
    if not its or ctx.rng.random() >= mix.p_hold:
        return True, ""

    pick = choose(ctx.cat, its, req)
    legs = pick["itinerary_id"].split("+")
    led.pending(legs, req.pax, +1)
    try:
        r = await ctx.client.call("POST", "/holds", json={"itinerary_id": pick["itinerary_id"], "passengers": req.pax})
    finally:
        led.pending(legs, req.pax, -1)
    ctx.rec.op("hold", r)
    timing[0] += _spent(r)
    if r.unknown:
        # The hold may exist on the airline's side. Treat its seats as taken until it would expire.
        led.add_hold(f"unknown-{uuid.uuid4().hex}", legs, req.pax)
        return False, f"hold got no answer ({r.error})"
    if r.status == 409:
        if ctx.checks and all(led.room(f) >= req.pax + SLACK for f in legs):
            ctx.rec.violation("sold out but seats remain",
                              f"{pick['itinerary_id']} for {req.pax}, Flight Control counts at least "
                              f"{min(led.room(f) for f in legs)} free")
        return True, "sold out"
    hold = r.body if isinstance(r.body, dict) else {}
    if r.status != 201 or not isinstance(hold.get("hold_id"), str) or not isinstance(hold.get("total_cents"), int):
        if r.status and r.status < 500:
            ctx.rec.violation("bad hold answer", f"{r.status} {str(r.body)[:120]}")
        return False, f"hold answered {r.status}"
    led.add_hold(hold["hold_id"], legs, req.pax)
    total = hold["total_cents"]
    if total != hold.get("price_cents", -1) * req.pax:
        ctx.rec.violation("hold total is not price times passengers", str(hold)[:120])

    if ctx.rng.random() < mix.p_abandon:
        return True, "abandoned hold"

    decline = ctx.rng.random() < mix.p_decline
    body = {"hold_id": hold["hold_id"], "passengers": ctx.names(req.pax),
            "payment_token": ctx.token(decline), "idempotency_key": ctx.key()}
    if ctx.rng.random() < mix.p_dup:
        rs = list(await asyncio.gather(ctx.client.call("POST", "/bookings", json=body),
                                       ctx.client.call("POST", "/bookings", json=body)))
    else:
        rs = [await ctx.client.call("POST", "/bookings", json=body)]
    for x in rs:
        ctx.rec.op("book", x)
        timing[0] += _spent(x)
    rb = rs[0]
    led.drop_hold(hold["hold_id"])
    # With a duplicate, both copies must be answered properly, not just one of them.
    dup_trouble = None
    if len(rs) > 1:
        kinds = []
        for x in rs:
            if x.unknown or x.status >= 500:
                dup_trouble = f"one copy of a repeated booking got {x.status or x.error}"
            elif x.status in (200, 201) and not (isinstance(x.body, dict)
                                                  and isinstance(x.body.get("booking_id"), str)):
                ctx.rec.violation("bad booking answer", str(x.body)[:120])
                dup_trouble = "one copy of a repeated booking had no booking_id"
            kinds.append("ok" if x.status in (200, 201) else str(x.status))
        # The contract says a repeat returns exactly the first answer, so the copies must match.
        def same(x, y) -> bool:
            sx, sy = (200 if x.status == 201 else x.status), (200 if y.status == 201 else y.status)
            return sx == sy and x.body == y.body
        if dup_trouble is None and not all(same(rs[0], x) for x in rs[1:]):
            ctx.rec.violation("repeated booking answered differently",
                              f"the same request got {rs[0].status} {str(rs[0].body)[:80]} and "
                              f"{rs[1].status} {str(rs[1].body)[:80]}")
            dup_trouble = "two copies of the same booking got different answers"
        if dup_trouble is None and len(set(kinds)) > 1:
            ctx.rec.violation("repeated booking answered differently",
                              f"the same request got {' and '.join(kinds)}")
            dup_trouble = "two copies of the same booking got different answers"
    booked = [x.body for x in rs if x.status in (200, 201) and isinstance(x.body, dict)
              and isinstance(x.body.get("booking_id"), str)]
    ids = {b["booking_id"] for b in booked}
    if len(ids) > 1:
        ctx.rec.violation("duplicate booking for one idempotency key", ", ".join(sorted(ids)))
    if any(x.unknown for x in rs) and not ids:
        led.mark_unknown(body["idempotency_key"], body, legs, req.pax, pick["itinerary_id"])
        return False, "booking got no answer"
    if ids:
        b = booked[0]
        if decline:
            ctx.rec.violation("declined payment was booked", b["booking_id"])
        if b.get("total_cents") != total:
            ctx.rec.violation("booking total differs from hold", f"{b.get('total_cents')} vs {total}")
        if b.get("flight_ids") != legs:
            ctx.rec.violation("booking flights differ from hold", f"{b.get('flight_ids')} vs {legs}")
        led.confirm(Booked(b["booking_id"], body["idempotency_key"], pick["itinerary_id"],
                           legs, req.pax, int(total), names=[p["name"] for p in body["passengers"]]))
        ctx.emit("book", origin=req.origin, destination=req.destination, pax=req.pax,
                 cents=total, stops=len(legs) - 1, date=req.day.isoformat())
        if ctx.rng.random() < mix.p_cancel:
            ctx.spawn(_cancel_later(ctx, b["booking_id"], mix))
        if dup_trouble:
            return False, dup_trouble
        return True, ""
    if rb.status == 402:
        if not decline:
            ctx.rec.violation("good payment declined", hold["hold_id"])
        if dup_trouble:
            return False, dup_trouble
        return True, "payment declined"
    if rb.status in (200, 201):
        ctx.rec.violation("bad booking answer", str(rb.body)[:120])
        return False, "booking answer had no booking_id"
    if rb.status and rb.status < 500:
        ctx.rec.violation("booking rejected", f"{rb.status} {str(rb.body)[:100]}")
    return False, f"booking answered {rb.status or rb.error}"


async def _cancel_later(ctx: Context, booking_id: str, mix: Mix) -> None:
    try:
        await asyncio.sleep(ctx.rng.uniform(*mix.cancel_delay))
        r = await ctx.client.call("DELETE", f"/bookings/{booking_id}")
        ctx.rec.op("cancel", r)
        if r.status == 200 and isinstance(r.body, dict) and r.body.get("status") == "cancelled":
            ctx.ledger.cancel(booking_id)
            ctx.emit("cancel", booking_id=booking_id)
        elif r.unknown or r.status >= 500:
            ctx.ledger.unknown_cancels.add(booking_id)
        else:
            ctx.rec.violation("cancel rejected", f"{r.status} for {booking_id}")
    except Exception as e:
        ctx.ledger.unknown_cancels.add(booking_id)
        ctx.rec.violation("unreadable answer", f"cancel {booking_id}: {type(e).__name__}"[:200])


# -- open-loop arrivals -------------------------------------------------------------

async def drive(ctx: Context, demand: Demand, mix: Mix, *, rate: Callable[[float], float],
                duration: float, start: date, span_days: int, max_inflight: int = 128,
                tag: Callable[[float], Any] | None = None,
                stop: Callable[[], bool] | None = None) -> None:
    """Start travelers on a Poisson schedule. Falling behind never slows the schedule."""
    t0 = time.monotonic()
    next_at = 0.0
    inflight: set[asyncio.Task] = set()
    while True:
        now = time.monotonic() - t0
        if now >= duration or (stop and stop()):
            break
        lam = rate(now)
        if lam <= 0:
            await asyncio.sleep(0.2)
            next_at = time.monotonic() - t0
            continue
        next_at += ctx.rng.expovariate(lam)
        wait = next_at - (time.monotonic() - t0)
        if wait > 0:
            await asyncio.sleep(wait)
        if len(inflight) >= max_inflight:
            ctx.rec.missed += 1
            ctx.rec.traveler(TravelerResult(ctx.rec.elapsed(), False, "not started, server behind",
                                            tag=tag(next_at) if tag else None))
            continue
        req = demand.request(ctx.rng, start, span_days)
        t = asyncio.create_task(traveler(ctx, req, mix, tag(next_at) if tag else None))
        inflight.add(t)
        t.add_done_callback(inflight.discard)
    # Every call has its own timeout, so waiting for all travelers is bounded.
    # Auditing before they finish would miss bookings that did go through.
    if inflight:
        await asyncio.wait(inflight)


def window_start(rng: random.Random, cat: Catalog, span_days: int) -> date:
    first = date(cat.year, 1, 8)
    last = date(cat.year, 12, 20) - timedelta(days=span_days)
    return first + timedelta(days=rng.randrange((last - first).days))


def iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
