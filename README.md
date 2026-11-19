# Flight Control

Flight Control is the traffic generator and grader for Projects 2 and 3 in
COMP 4340/6340 (Advanced Database Systems) at Northwest Nazarene University.

Each team builds an airline booking service that follows
[CONTRACT.md](CONTRACT.md) and runs it on their own machine. Flight Control
connects to it at `localhost` and plays the part of thousands of travelers.
It searches, holds seats, books, cancels and audits, and it checks every
answer against its own record of what happened.

## For students

Read [CONTRACT.md](CONTRACT.md) first. It is the whole specification. How you
build the airline behind it is up to you.

Start with the Getting started section of the contract. It runs a small toy
airline from `examples/` and shows Flight Control calling it, one line per
call, so you can see the whole loop work before you write anything.

Run every test on your own machine against your airline on `localhost`.
Flight Control's command-line grader needs no public URL, tunnel, Docker
Compose setup, `.env`, or admin password.

```
git clone https://github.com/enochlev/flight-control
cd flight-control
uv sync
uv run flightcontrol test --url http://localhost:8080 --test 1 --verbose
```

`--test` takes 1 to 5. Use `--seed` to run a test with a different seed.

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

## Optional instructor dashboard

The dashboard is separate from the command-line grader. To try it locally,
run `uv run flightcontrol web` and `uv run flightcontrol engine` in separate
terminals from the repository root. Set `FC_DEV=1` in both terminals and set
`FC_ADMIN_PASSWORD` in the web terminal. Open
`http://127.0.0.1:8000/admin` and sign in as `admin`. Create a team, save
your airline's localhost URL on the team page, and approve it in the dashboard.
Use `FC_DEV=1` only for a trusted local dashboard.

For a hosted dashboard, Docker Compose can run the web and engine processes
with a shared SQLite file. Copy `.env.example` to `.env`, set
`FC_ADMIN_PASSWORD`, and configure `FC_PUBLIC_URL` and
`FC_ALLOWED_HOST_SUFFIXES` for your deployment. The domain list is empty
by default.

```bash
docker compose up -d --build
```

The container publishes the dashboard at `127.0.0.1:8000`. Serve it over
HTTPS if you make it public. For hosted runs, Flight Control only calls an
instructor-approved HTTPS URL on an explicitly allowed domain that resolves
to a public address. Flight Control never follows redirects.
