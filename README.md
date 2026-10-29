# Flight Control

Flight Control is the traffic generator and grader for Projects 2 and 3 in
COMP 4340/6340 (Advanced Database Systems) at Northwest Nazarene University.

Each team builds an airline booking service that follows
[CONTRACT.md](CONTRACT.md). Teams run it on their own machines and expose it
through a Cloudflare tunnel. Flight Control then plays the part of thousands
of travelers. It searches, holds seats, books, cancels and audits, and it
checks every answer against its own record of what happened.

## For students

Read [CONTRACT.md](CONTRACT.md) first. It is the whole specification. How you
build the airline behind it is up to you.

Start with the Getting started section of the contract. It runs a small toy
airline from `examples/` and shows Flight Control calling it, one line per
call, so you can see the whole loop work before you write anything.

You run every test on your own machine against your own server on
`localhost`.

```
git clone https://github.com/enochlev/flight-control
cd flight-control
uv sync
uv run flightcontrol test --url http://localhost:8080 --key <your key> --test 1 --verbose
```

`--test` takes 1 to 5. The class site runs the same tests with different
seeds, so a local pass is a good sign but not a promise.

## What is in this repository

| Path | What it is |
|---|---|
| `CONTRACT.md` | the API every airline serves, and how to get started |
| `examples/toy_airline.py` | a demo airline with no database, for seeing the loop work |
| `src/flightcontrol/catalog.py` | builds the flight catalog from a fixed seed |
| `src/flightcontrol/oracle.py` | the rules for a valid itinerary |
| `src/flightcontrol/workload.py` | travelers, and the checks on every answer |
| `src/flightcontrol/grading.py` | tests 1 to 5 |
| `src/flightcontrol/engine.py` | the process that sends all traffic |
| `src/flightcontrol/web.py` | the web site |

## Running the site

The site and the engine are two processes that share one SQLite file.

```
cp .env.example .env      # then set FC_ADMIN_PASSWORD
docker compose up -d --build
```

The web site listens on 127.0.0.1:8000. Put it behind a Cloudflare named
tunnel and put `/admin` behind Cloudflare Access. Create teams on `/admin`
and hand each team its private link.

Flight Control only calls a team URL after the instructor approves that exact
URL, and only if it is https, ends in an allowed domain
(`FC_ALLOWED_HOST_SUFFIXES`) and resolves to a public address. It never
follows redirects.
