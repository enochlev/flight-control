# Airline service

The airline runs independently of the Flight Control grader. With Docker and
the Compose plugin installed, start it from this directory.

```sh
docker compose up
```

In another terminal, check that the API can reach PostgreSQL.

```sh
curl -fsS -H 'Authorization: Bearer local-airline-key' http://localhost:8080/health
```

## Configuration

Set these variables in an `airline/.env` file or in the shell before starting
Compose. The defaults are for local development.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AIRLINE_API_KEY` | `local-airline-key` | Bearer key the client sends. Set your own key before exposing the API through a tunnel. |
| `AIRLINE_PORT` | `8080` | Host port. Use `http://localhost:<port>` as the local base URL. |
| `AIRLINE_DB_PASSWORD` | `local-airline-db` | Password used by the API and when PostgreSQL initializes a fresh volume. |

Compose publishes only the API on loopback. For a remote grader, point the
tunnel at the local base URL and give the grader the tunnel URL and the same
bearer key. Changing the database password variable on an existing volume
also requires changing the password of the PostgreSQL role.

Compose waits for PostgreSQL before starting the API and restarts the API
container if its process exits. The `airline_data` volume survives
`docker compose down` and a subsequent `docker compose up`. Running
`docker compose down -v` deletes that volume and its data.

## Stack decision for the paper

- **Python 3.12 and FastAPI** use the same language and web framework as the
  grader. FastAPI gives us request validation and JSON responses for the API.
- **PostgreSQL and Psycopg** give us database transactions and row-level locks.
  A future hold or booking can update both legs of an itinerary in one
  transaction, while reservations for unrelated flights can proceed
  concurrently. PostgreSQL adds a separate process and connection management.
  Compose handles startup and gives the database a persistent volume.

The scaffold provides an authenticated health check. The flight catalog,
booking endpoints, and `POST /admin/restart` are tracked separately.
