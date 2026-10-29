"""A toy airline that answers every endpoint in CONTRACT.md.

This is NOT a starting point for your project. It keeps everything in Python
dictionaries, so it has no database, and when it restarts every booking is gone.
It exists so you can see Flight Control talk to a working server in five minutes
before you build your own. Test 4 (Restart) fails against it on purpose.

Run it:
    uv run --with fastapi --with uvicorn uvicorn examples.toy_airline:app --port 8080
Then, in a second terminal:
    uv run flightcontrol test --url http://localhost:8080 --key toy-key-123 --test 1 --verbose
"""

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from flightcontrol import catalog, oracle

API_KEY = os.environ.get("TOY_KEY", "toy-key-123")
BOOT_ID = str(uuid.uuid4())
HOLD_SECONDS = 60

cat = catalog.get()
sold: dict[str, int] = {}        # flight_id -> seats sold
holds: dict[str, dict] = {}      # hold_id -> hold
bookings: dict[str, dict] = {}   # booking_id -> booking
replies: dict[str, tuple] = {}   # idempotency key -> (request body, status, reply)

app = FastAPI()


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def held(fid: str) -> int:
    t = now()
    return sum(h["passengers"] for h in holds.values() if fid in h["flight_ids"] and h["expires"] > t)


def available(fid: str) -> int:
    return cat.by_id[fid].capacity - sold.get(fid, 0) - held(fid)


@app.middleware("http")
async def key_and_timing(request: Request, call_next):
    start = time.perf_counter()
    if request.headers.get("authorization") != f"Bearer {API_KEY}":
        response = error(401, "unauthorized")
    else:
        response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "boot_id": BOOT_ID, "version": "toy"}


@app.get("/search")
def search(origin: str, destination: str, date: str, passengers: int = 1):
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return error(422, "bad_date")
    found = []
    for it in oracle.itineraries(cat, origin, destination, day):
        seats = min(available(f.flight_id) for f in it.legs)
        if seats >= passengers:
            found.append({"itinerary_id": it.itinerary_id, "flight_ids": [f.flight_id for f in it.legs],
                          "departure": iso(it.departure), "arrival": iso(it.arrival), "stops": it.stops,
                          "price_cents": it.base_fare_cents, "seats_available": seats})
    return {"itineraries": found[:20]}


@app.post("/holds")
async def make_hold(request: Request):
    body = await request.json()
    ids = str(body.get("itinerary_id", "")).split("+")
    pax = body.get("passengers")
    if not isinstance(pax, int) or not 1 <= pax <= 9:
        return error(422, "bad_request")
    if any(fid not in cat.by_id for fid in ids):
        return error(404, "unknown_flight")
    if any(available(fid) < pax for fid in ids):
        return error(409, "sold_out")
    price = sum(cat.by_id[fid].base_fare_cents for fid in ids)
    hold = {"hold_id": "h_" + uuid.uuid4().hex[:10], "itinerary_id": body["itinerary_id"], "flight_ids": ids,
            "passengers": pax, "price_cents": price, "total_cents": price * pax,
            "expires": now() + timedelta(seconds=HOLD_SECONDS)}
    holds[hold["hold_id"]] = hold
    shown = {k: v for k, v in hold.items() if k not in ("expires", "flight_ids")}
    return JSONResponse({**shown, "expires_at": iso(hold["expires"])}, status_code=201)


@app.post("/bookings")
async def book(request: Request):
    body = await request.json()
    key = body.get("idempotency_key")
    if key in replies:
        first, status, reply = replies[key]
        if first != body:
            return error(422, "idempotency_key_reused")
        return JSONResponse(reply, status_code=200 if status == 201 else status)
    hold = holds.pop(body.get("hold_id"), None)
    if hold is None or hold["expires"] <= now():
        status, reply = 410, {"error": "hold_expired", "detail": ""}
    elif len(body.get("passengers") or []) != hold["passengers"]:
        holds[hold["hold_id"]] = hold
        status, reply = 422, {"error": "passenger_mismatch", "detail": ""}
    elif not str(body.get("payment_token", "")).startswith("tok_ok_"):
        status, reply = 402, {"error": "payment_declined", "detail": ""}
    else:
        for fid in hold["flight_ids"]:
            sold[fid] = sold.get(fid, 0) + hold["passengers"]
        booking = {"booking_id": "b_" + uuid.uuid4().hex[:10], "status": "confirmed",
                   "itinerary_id": hold["itinerary_id"], "flight_ids": hold["flight_ids"],
                   "passengers": body["passengers"], "total_cents": hold["total_cents"], "created_at": iso(now())}
        bookings[booking["booking_id"]] = booking
        status, reply = 201, booking
    replies[key] = (body, status, reply)
    return JSONResponse(reply, status_code=status)


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    return bookings.get(booking_id) or error(404, "not_found")


@app.delete("/bookings/{booking_id}")
def cancel(booking_id: str):
    b = bookings.get(booking_id)
    if b is None:
        return error(404, "not_found")
    if b["status"] == "confirmed":
        for fid in b["flight_ids"]:
            sold[fid] -= len(b["passengers"])
        b["status"] = "cancelled"
    return b


@app.get("/admin/stats")
def stats():
    live = [h for h in holds.values() if h["expires"] > now()]
    confirmed = [b for b in bookings.values() if b["status"] == "confirmed"]
    return {"bookings_confirmed": len(confirmed),
            "bookings_cancelled": sum(b["status"] == "cancelled" for b in bookings.values()),
            "seats_sold": sum(len(b["passengers"]) for b in confirmed),
            "revenue_cents": sum(b["total_cents"] for b in confirmed),
            "active_holds": len(live), "seats_held": sum(h["passengers"] for h in live)}


@app.get("/admin/flights/{flight_id}")
def flight(flight_id: str):
    f = cat.by_id.get(flight_id)
    if f is None:
        return error(404, "not_found")
    return {"flight_id": flight_id, "capacity": f.capacity, "seats_sold": sold.get(flight_id, 0),
            "seats_held": held(flight_id), "seats_available": available(flight_id)}


@app.get("/admin/bookings")
def all_bookings(offset: int = 0, limit: int = 500):
    ordered = sorted(bookings.values(), key=lambda b: (b["created_at"], b["booking_id"]))
    return {"total": len(ordered), "bookings": ordered[offset:offset + limit]}


@app.post("/admin/reset")
def reset():
    sold.clear(), holds.clear(), bookings.clear(), replies.clear()
    return {"status": "reset"}


@app.post("/admin/restart")
async def restart():
    asyncio.get_running_loop().call_later(0.3, os._exit, 1)  # and with it, every booking
    return JSONResponse({"status": "restarting"}, status_code=202)
