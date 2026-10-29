# Flight Control API contract, version 1.0

You are building the reservation system for one airline. Its whole schedule
is in the catalog below. Flight Control plays the part of thousands of
travelers. It calls these endpoints on your server, checks every answer
against the catalog, and keeps its own record of every booking it was told
about so it can audit you afterwards.

You run Flight Control on your own computer against your own server on
`localhost`. The class site at https://flights.enochlev.com shows this contract
and gives each team a private page where it can also run the tests through a
tunnel. Runs on your own machine never show up there.

How you build the system behind the API is up to you. That includes which
database holds what, how you index it and how you keep it correct under load.

Anything not written here is your decision. If something here is unclear,
ask in class or on Canvas before you build around a guess. Changes after
version 1.0 will be listed at the bottom with a date.

## Getting started

These steps get a working airline talking to Flight Control in about five
minutes, so you can see what the traffic looks like before you build anything.

**Step 1.** Install [uv](https://docs.astral.sh/uv/), then clone this
repository and install it.

```
git clone https://github.com/enochlev/flight-control
cd flight-control
uv sync
```

**Step 2.** Start the toy airline in one terminal. It answers every endpoint
in this contract, but it keeps everything in memory with no database. It is a
demo, not a starting point.

```
uv run --with fastapi --with uvicorn uvicorn examples.toy_airline:app --port 8080
```

**Step 3.** In a second terminal, run test 1 with `--verbose` to watch every
call go by.

```
uv run flightcontrol test --url http://localhost:8080 --key toy-key-123 --test 1 --verbose
```

Each line is one call, its status and how long it took. If your server is not
running you will see `connect` instead of a status.

**Step 4.** Run test 4 against the toy airline. Its restart endpoint stops the
process, and nothing starts it again, so the test reports that it never came
back. Even if something did restart it, every booking it kept in memory would
be gone. Your airline has to come back on its own with every booking intact.

```
uv run flightcontrol test --url http://localhost:8080 --key toy-key-123 --test 4
```

**Step 5.** Build your own airline that serves the same endpoints, start it on
a port, and point `--url` at it.

## A worked example

One traveler, start to finish. Every request also carries the
`Authorization` header.

```
GET /search?origin=BOI&destination=JFK&date=2027-03-15&passengers=2
  200  {"itineraries": [{"itinerary_id": "FC160-20270315+FC263-20270315", "price_cents": 38000,
                         "seats_available": 9, ...}, ...]}

POST /holds  {"itinerary_id": "FC160-20270315+FC263-20270315", "passengers": 2}
  201  {"hold_id": "h_81c2", "total_cents": 76000, "expires_at": "...", ...}

POST /bookings  {"hold_id": "h_81c2", "passengers": [{"name": "Ada Park"}, {"name": "Lee Park"}],
                 "payment_token": "tok_ok_5521", "idempotency_key": "fc-7d2e"}
  201  {"booking_id": "b_1a9f", "status": "confirmed", ...}

DELETE /bookings/b_1a9f
  200  {"booking_id": "b_1a9f", "status": "cancelled", ...}
```

Two seats were taken on both flights by the hold, sold by the booking, and
given back by the cancel.

## Basics

- Every request carries `Authorization: Bearer <your API key>`. Reject
  requests without it with `401`. You pick the key and enter it on your
  team page. Treat it as low value and change it if it leaks.
- Bodies are JSON. Times are UTC in ISO 8601 with a `Z`, for example
  `2027-03-15T14:05:00Z`. Dates are `YYYY-MM-DD`. Money is integer cents.
- Errors return a JSON body `{"error": "<code>", "detail": "<text>"}` with the
  status codes given below. A request with a missing field or a field of the
  wrong type returns `422`.
- Every response must include a `Server-Timing` header with your handling
  time, for example `Server-Timing: app;dur=12.4` (milliseconds). Test 2
  scores this number so that tunnel and Wi-Fi delay do not count against you.
- Flight Control waits at most 2 seconds for a response. A request that times
  out is treated as **unknown**, not failed. Flight Control will retry unknown
  bookings with the same idempotency key before it audits you.
- If `Server-Timing` is missing, or reports more time than the whole round
  trip took, Flight Control uses the round trip instead. Every call, including
  a repeated booking, adds its time to the traveler's total.
- Do not rate limit Flight Control.
- Keep bookings, holds and seat counts in a database. Anything kept only in
  your application's memory is gone after a restart, and test 4 checks for it.

## The catalog

The catalog holds every scheduled flight for 2027 across 15 airports, all
flown by your one airline. Columns are listed below.

| column | meaning |
|---|---|
| `flight_id` | unique per departure, for example `FC1203-20270315` |
| `flight_number` | the same every day, for example `FC1203` |
| `origin`, `destination` | airport codes |
| `scheduled_departure`, `scheduled_arrival` | UTC |
| `origin_local_date` | the departure date in the origin airport's time zone |
| `capacity` | seats on the flight |
| `base_fare_cents` | a reference fare per passenger |
| `distance_mi` | great circle miles |

Write it out with `uv run flightcontrol catalog --out catalog`. That writes
`catalog/flights.csv` and `catalog/airports.csv`, and prints the number of
flights (175,224) and the file's SHA-256 hash. Every computer produces the same
file, and the catalog does not change during the term.

Planes are small on purpose, between 10 and 23 seats. The tests send a few
thousand travelers at a time, and at this size some flights sell out during a
test while most travelers still find a seat. Load `capacity` exactly as it is.
Flight Control compares the capacity you report on `/admin/flights` with the
catalog.

## Itineraries

An itinerary is one flight (nonstop) or two flights (one stop). There are
never two stops, in Project 2 or Project 3.

- The first flight leaves the requested origin on the requested
  `origin_local_date`.
- For a connection, the second flight leaves the airport where the first one
  lands, between 45 and 240 minutes after it lands, both ends included. It may
  leave on the next calendar day.
- An itinerary never goes back through its origin.
- `itinerary_id` is the flight ids joined with `+`, in order, for example
  `FC1203-20270315+FC1544-20270315`. Flight Control may hold an itinerary it
  built itself without searching first.

## Seats

For every flight, at every moment,

```
seats_available = capacity - seats_sold - seats_held
```

where `seats_held` counts only holds that have not expired. `seats_available`
may never go below zero. A booking that would push any flight below zero is
an **oversell** and fails the test it happened in.

A hold or booking for an itinerary takes the same number of seats on every
flight in it. Either every flight gets the seats or none does. A booking that
ends up on one leg but not the other is a **partial itinerary** and also fails
the test.

## Endpoints

### `GET /health`

```json
{"status": "ok", "boot_id": "6f1c...", "version": "anything you like"}
```

`boot_id` must be a new random value every time your application process
starts. Flight Control uses it to confirm that a restart really happened.

### `GET /search?origin=BOI&destination=JFK&date=2027-03-15&passengers=2`

```json
{"itineraries": [
  {"itinerary_id": "FC160-20270315+FC263-20270315",
   "flight_ids": ["FC160-20270315", "FC263-20270315"],
   "departure": "2027-03-15T13:00:00Z",
   "arrival": "2027-03-15T20:40:00Z",
   "stops": 1,
   "price_cents": 41200,
   "seats_available": 37}
]}
```

- Return at most 20 itineraries, in any order you think travelers want.
- Every itinerary must be valid under the rules above and have
  `seats_available >= passengers`. Offering a full flight counts as an error.
- If 3 or more valid itineraries have room, return at least 3. If fewer
  exist, return all of them.
- Every nonstop with room must be included.
- `price_cents` is per passenger and is your choice. It may change over time.
  The catalog's `base_fare_cents` is a sensible default, summed over the
  flights in the itinerary.
- `seats_available` is the smallest value across the itinerary's flights.
  It may be up to 5 seconds stale. Holds are where it has to be exact.
- Unknown airports or a bad date return `422`.

### `POST /holds`

A hold is what an airline website does while you type in names and payment.
The seats are set aside for you for a short time so nobody else can take
them, but nothing is sold yet. If the payment fails or the traveler walks
away, the hold runs out and the seats go back on sale. This is where two
travelers racing for the last seat meet, so it is where your locking has to
be right.

Request `{"itinerary_id": "...", "passengers": 2}` with 1 to 9 passengers.

A successful hold answers `201` with this body.

```json
{"hold_id": "h_81c2", "itinerary_id": "...", "passengers": 2,
 "price_cents": 41200, "total_cents": 82400,
 "expires_at": "2026-11-05T16:21:07Z"}
```

- A hold reserves the seats on every flight for **60 seconds of real time**.
  Downtime counts. A hold made just before a restart may expire while you are
  down.
- The price is locked at hold time. The booking charges `total_cents`.
- `409` if any flight lacks the seats. `404` if a flight id is unknown.
  `422` if the itinerary breaks the connection rules.

### `POST /bookings`

The request looks like this.

```json
{"hold_id": "h_81c2",
 "passengers": [{"name": "Ada Park"}, {"name": "Lee Park"}],
 "payment_token": "tok_ok_5521",
 "idempotency_key": "fc-7d2e..."}
```

A successful booking answers `201` with this body.

```json
{"booking_id": "b_1a9f", "status": "confirmed", "itinerary_id": "...",
 "flight_ids": ["..."], "passengers": [{"name": "Ada Park"}, {"name": "Lee Park"}],
 "total_cents": 82400, "created_at": "2026-11-05T16:20:31Z"}
```

- The number of names must match the hold, otherwise `422`.
- Payment tokens starting with `tok_ok_` succeed. Tokens starting with
  `tok_decline_` fail with `402`, and the hold is released.
- An expired or unknown hold returns `410`. A hold can be booked once.
- **Idempotency.** The same `idempotency_key` with the same body must return
  the same answer as the first time and must never create a second booking.
  This includes two copies arriving at the same moment. A repeat of a
  successful booking may answer `200` or `201`. Return the answer exactly as
  you first sent it, even if the booking was cancelled since. The same key
  with a different body returns `422`. Keep keys for at least 24 hours.

### `GET /bookings/{booking_id}`

Returns the booking as above, with `status` either `confirmed` or
`cancelled`. `404` if unknown.

### `DELETE /bookings/{booking_id}`

Cancels the booking, returns its seats on every flight and refunds it in
full. Returns the booking with `status` `cancelled`. Cancelling twice is not
an error and returns the same thing. `404` if unknown.

### Admin endpoints

These exist so Flight Control can audit you.

`GET /admin/stats`

```json
{"bookings_confirmed": 912, "bookings_cancelled": 40, "seats_sold": 1730,
 "revenue_cents": 51200400, "active_holds": 12, "seats_held": 19}
```

Revenue counts confirmed bookings only. `seats_sold` here counts passengers
in confirmed bookings, so a passenger on a connection counts once.
`seats_held` counts passengers in holds that have not expired.

`GET /admin/flights/{flight_id}`

```json
{"flight_id": "...", "capacity": 16, "seats_sold": 9, "seats_held": 2,
 "seats_available": 5}
```

Here `seats_sold` and `seats_held` count passengers on this one flight, so a
passenger on a connection counts once on each of its two flights. All three
numbers must be whole numbers, and `seats_available` must equal `capacity`
minus `seats_sold` minus `seats_held`.

`GET /admin/bookings?offset=0&limit=500` returns
`{"total": 952, "bookings": [...]}` with every booking, confirmed and
cancelled, ordered by `created_at` and then `booking_id`.

`POST /admin/reset` deletes every booking and hold, then answers `200`.
It must finish within 30 seconds. Flight Control resets you before each
graded test.

`POST /admin/restart` answers `202` within 1 second. Then your application
process must exit without a clean shutdown, as if the power went out, and
start again on its own (a Docker restart policy does this). Stay down for
about 30 seconds and be healthy again within 60 seconds of the request, with a
new `boot_id`. Flight Control checks `/health` every second, and it has to see
your server down at least once, so a new `boot_id` without going down does not
count. Keep your tunnel running while the app restarts.
Every booking you confirmed before the restart must still be there after it,
with the same status, flights, passengers and total.
Whether your database also crashes, and how it recovers, is something you
explain in your paper and the instructor checks by hand.

## Tests

Flight Control's traffic is made of **travelers**. A traveler searches, then
sometimes holds, then sometimes books, and sometimes cancels later. The mix
and rates for each test are below. Arrivals follow a fixed schedule. If your
server falls behind, Flight Control does not slow down for you.

Travelers pick from what your search returns the way people do. Each one
weighs the price against the time the trip takes and the hassle of a
connection. About 70 percent are leisure travelers, who value an hour at about
$15 and a connection at about $60. The rest are business travelers, who value
an hour at about $75 and a connection at about $150. So a long connecting trip
gets chosen only when it is cheap enough to make up for it.

Tests 2, 4 and 5 send every traveler to flights on one travel day, so
popular flights fill up and later travelers have to take their second choice.

A traveler is **correct** when every call it made got an answer (no timeout
and no 5xx), none of your answers broke a rule in this contract, and its whole
trip took under 5 seconds. A sold-out answer or a declined payment is a
correct outcome when it is the right answer. When Flight Control sends the
same booking twice, both copies need the same answer, either both the
booking or both the same error. Cancellations happen a
little later, so they are checked by the audit rather than counted against
the traveler. Each failed traveler is reported with its reason.

| Test | What happens | Must hold (otherwise the test scores 0) | Score from 0 to 1 |
|---|---|---|---|
| 1 Steady | 1 traveler per second, always on while you are connected | nothing, it is for watching | not scored |
| 2 Rush | travelers per second step up 5, 10, 20, 30, 40, 30 seconds each step | no oversell, no partial itinerary | highest step where 95% of travelers are correct and the p95 of each correct traveler's server time, added up over its calls, is under 500 ms. Divided by 40 |
| 3 Last seat | 10 rounds. A flight is filled to within 1 to 3 seats, then 40 travelers try to book it at once | exactly the remaining seats sold, every loser gets `409` | rounds passed divided by 10 |
| 4 Restart | 5 travelers per second for 3 minutes, restart called at 60 seconds | every confirmed booking still there, new `boot_id`, healthy within 60 seconds | fraction of travelers correct after you come back |
| 5 Audit | 10 travelers per second for 3 minutes with duplicate requests, cancellations, declined payments and abandoned holds, then a 70 second drain | no answer breaks a rule, at least 95% of travelers correct, at least 100 bookings made, and stats, per-flight seats and the booking export all match Flight Control's record exactly | 1 if everything holds |

Each test gives a score from 0 to 1, and the project assignment says how the
scores count toward your grade. Hidden tests use the same rules with different
flights, seeds and timing.

## Running the tests yourself

```
uv run flightcontrol test --url http://localhost:8080 --key <your key> --test 3
```

At the end it prints the score, every rule that failed, why travelers failed,
and examples. Add `--verbose` to print every call, or `--json` to print only
the full result as JSON. Test 1 runs for two minutes unless you pass
`--minutes`.

## Changes

- Oct 29 2026, version 1.0 published. Later the same day the rules for a
  correct traveler, the Audit and Restart pass conditions, seat counting and
  repeated bookings were written out in full.
