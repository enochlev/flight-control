import os
import secrets
import time
import uuid

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

AUTHORIZATION = f"Bearer {os.environ['AIRLINE_API_KEY']}".encode()
BOOT_ID = str(uuid.uuid4())

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    started = time.perf_counter()
    authorization = request.headers.get("authorization", "").encode("latin-1")
    if secrets.compare_digest(authorization, AUTHORIZATION):
        response = await call_next(request)
    else:
        response = JSONResponse({"error": "unauthorized", "detail": ""}, status_code=401)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.get("/health")
def health():
    try:
        with psycopg.connect() as connection:
            connection.execute("SELECT 1")
    except psycopg.OperationalError:
        return JSONResponse({"error": "database_unavailable", "detail": ""}, status_code=503)
    return {"status": "ok", "boot_id": BOOT_ID, "version": "0.1.0"}
