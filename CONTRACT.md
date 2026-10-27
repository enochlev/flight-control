# Flight Control API contract, version 1.0

This is the API your airline must serve. Flight Control plays the part of
thousands of travelers. It calls these endpoints on your server through your
tunnel, and it keeps its own record of every booking it was told about.
How you build the system behind the API is up to you. That includes which
database holds what, how you index it and how you keep it correct under load.

Anything not written here is your decision. If something here is unclear,
ask in class or on Canvas before you build around a guess. Changes after
version 1.0 will be listed at the bottom with a date.

## Basics

- Every request carries `Authorization: Bearer <your API key>`. Reject
  requests without it with `401`. You pick the key and enter it on your
  team page. Treat it as low value and change it if it leaks.
- Bodies are JSON. Times are UTC in ISO 8601 with a `Z`, for example
  `2027-03-15T14:05:00Z`. Dates are `YYYY-MM-DD`. Money is integer cents.
- Errors return a JSON body `{"error": "<code>", "detail": "<text>"}` with the
  status codes given below.
- Every response must include a `Server-Timing` header with your handling
  time, for example `Server-Timing: app;dur=12.4` (milliseconds). The
  leaderboard scores this number so that tunnel and Wi-Fi delay do not count
  against you. Flight Control also measures the full round trip and will flag
  numbers that do not add up.
- Flight Control waits at most 2 seconds for a response. A request that times
  out is treated as **unknown**, not failed. Flight Control will retry unknown
  bookings with the same idempotency key before it audits you.
- Do not rate limit Flight Control.

## The catalog

Download it from Flight Control (`/catalog/flights.csv` and
`/catalog/airports.csv`). It holds every scheduled flight for 2027 across 15
airports. Columns are listed below.

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

The catalog does not change during the term. Its version and SHA-256 hash are
shown on Flight Control's front page.

## Itineraries

An itinerary is one flight (nonstop) or two flights (one stop).

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
- `seats_available` is the smallest value across the itinerary's flights.
  It may be a few seconds stale. Holds are where it has to be exact.
- Unknown airports or a bad date return `422`.

### `POST /holds`

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
  the same booking (status `200` or `201`) and must never create a second
  one. This includes two copies arriving at the same moment. The same key
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

Revenue counts confirmed bookings only.

`GET /admin/flights/{flight_id}`

```json
{"flight_id": "...", "capacity": 126, "seats_sold": 40, "seats_held": 2,
 "seats_available": 84}
```

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
new `boot_id`. Keep your tunnel running while the app restarts.
Every booking you confirmed before the restart must still be there after it.
Whether your database also crashes, and how it recovers, is something you
explain in your paper and the instructor checks by hand.

## Tests

Flight Control's traffic is made of **travelers**. A traveler searches, then
sometimes holds, then sometimes books, and sometimes cancels later. The mix
and rates for each test are below. Arrivals follow a fixed schedule. If your
server falls behind, Flight Control does not slow down for you.

| Test | What happens | Must hold (otherwise the test scores 0) | Score from 0 to 1 |
|---|---|---|---|
| 1 Steady | 1 traveler per second, always on while you are connected | nothing, it is for watching | not scored |
| 2 Rush | travelers per second step up 5, 10, 20, 30, 40, 30 seconds each step | no oversell, no partial itinerary | highest step with 95% of travelers correct and p95 `Server-Timing` under 500 ms, divided by 40 |
| 3 Last seat | 10 rounds. A flight is filled to within 1 to 3 seats, then 40 travelers try to book it at once | exactly the remaining seats sold, every loser gets `409` | rounds passed divided by 10 |
| 4 Restart | 5 travelers per second for 3 minutes, restart called at 60 seconds | every confirmed booking still there, new `boot_id`, healthy within 60 seconds | fraction of travelers correct after you come back |
| 5 Audit | 10 travelers per second for 3 minutes with duplicate requests, cancellations, declined payments and abandoned holds, then a 70 second drain | stats, per-flight seats and the booking export all match Flight Control's record exactly | 1 if everything matches |

Leaderboard score is `40 x rush + 20 x last seat + 20 x restart + 20 x audit`.
It uses your best of your last three runs of each test. Hidden tests use the
same rules with different flights, seeds and timing.

## Running the tests yourself

The same engine runs on your own machine against `localhost`, so you do not
need the tunnel while you build.

```
uv run flightcontrol test --url http://localhost:8080 --key <your key> --test 3
```

## Changes

None yet.
