"""The Flight Control web site. It reads and writes SQLite and never calls a team.

Public pages show team names and live numbers only, never URLs or keys.
A team's own page lives at /t/<token>, a secret link the instructor hands out.
Instructor pages sit under /admin behind HTTP Basic auth, and should also sit
behind Cloudflare Access in production.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import catalog, store
from .client import UnsafeURL, check_url

HERE = Path(__file__).parent
DEV = os.environ.get("FC_DEV") == "1"
ADMIN_PASSWORD = os.environ.get("FC_ADMIN_PASSWORD", "")
SECRET = os.environ.get("FC_SECRET") or hashlib.sha256(("fc" + ADMIN_PASSWORD).encode()).hexdigest()

app = FastAPI(title="Flight Control", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2000)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["TEST_NAMES"] = store.TEST_NAMES
templates.env.filters["ago"] = lambda ts: _ago(ts)
templates.env.filters["pct"] = lambda x: "" if x is None else f"{x * 100:.0f}%"
basic = HTTPBasic(auto_error=False)


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return time.strftime("%b %d %H:%M", time.localtime(ts))


@app.on_event("startup")
def _startup() -> None:
    store.init()
    _catalog_files()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


# -- catalog ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def _catalog_files() -> dict:
    cat = catalog.get()
    flights = cat.to_csv().encode()
    airports = catalog.airports_csv().encode()
    return {"flights": flights, "airports": airports,
            "sha256": hashlib.sha256(flights).hexdigest(), "count": len(cat.flights),
            "routes": len(catalog.routes()), "year": cat.year}


@app.get("/catalog/flights.csv")
def flights_csv():
    return Response(_catalog_files()["flights"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=flights.csv"})


@app.get("/catalog/airports.csv")
def airports_csv():
    return Response(_catalog_files()["airports"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=airports.csv"})


# -- public pages ------------------------------------------------------------------

def _airports() -> list[dict]:
    return [{"code": a.code, "city": a.city, "lat": a.lat, "lon": a.lon, "hub": a.hub}
            for a in catalog.AIRPORT_BY_CODE.values()]


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"cat": _catalog_files(), **ctx})


@app.get("/", response_class=HTMLResponse)
def live(request: Request):
    return _page(request, "live.html", airports=_airports(), nav="live")


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request):
    rows = [r for r in store.leaderboard() if r["has_runs"] or r["is_house"]]
    return _page(request, "leaderboard.html", rows=rows, weights=store.WEIGHTS, nav="leaderboard")


@lru_cache(maxsize=1)
def _contract_html() -> str:
    for p in (HERE.parent.parent / "CONTRACT.md", Path("/app/CONTRACT.md")):
        if p.exists():
            return markdown.markdown(p.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "toc"])
    return "<p>CONTRACT.md is missing from this deployment.</p>"


@app.get("/contract", response_class=HTMLResponse)
def contract(request: Request):
    return _page(request, "contract.html", body=_contract_html(), nav="contract")


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request):
    return _page(request, "catalog.html", airports=_airports(), nav="catalog")


def _public_team(t: dict) -> dict:
    keep = ("name", "house", "mode", "phase", "rate", "ok", "bad", "p95_ms", "bookings")
    return {k: t.get(k) for k in keep}


@app.get("/api/live")
def api_live():
    snap, updated = store.get("live", {})
    snap = snap or {}
    return {"updated": updated, "stale": time.time() - updated > 10,
            "teams": [_public_team(t) for t in snap.get("teams", [])],
            "feed": [{k: e.get(k) for k in ("t", "team", "house", "kind", "origin", "destination",
                                            "pax", "cents", "stops", "text", "test")}
                     for e in snap.get("feed", []) if e.get("kind") in ("book", "cancel")][-40:],
            "arcs": snap.get("arcs", []), "bookings_per_min": snap.get("bookings_per_min", 0)}


# -- team pages ------------------------------------------------------------------------

def _team_or_404(token: str) -> dict:
    t = store.team_by_token(token)
    if not t or t["is_house"]:
        raise HTTPException(404)
    return t


def _mask(key: str) -> str:
    return "" if not key else key[:3] + "•" * 6 + key[-2:] if len(key) > 6 else "•" * len(key)


@app.get("/t/{token}", response_class=HTMLResponse)
def team_page(request: Request, token: str, msg: str = "", err: str = ""):
    t = _team_or_404(token)
    runs = store.runs_for(t["id"], 30)
    return _page(request, "team.html", team=t, runs=runs, key_mask=_mask(t["api_key"]),
                 ready=store.url_ready(t), pending=bool(t["url"]) and t["url"] != t["approved_url"],
                 practice_left=max(0, store.PRACTICE_RUNS_PER_DAY - store.practice_runs_today(t["id"])),
                 active=store.active_run(t["id"]), msg=msg, err=err, token=token)


def _back(token: str, **q: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/t/{token}?{urlencode(q)}", status_code=303)


@app.post("/t/{token}/settings")
def team_settings(token: str, url: str = Form(""), api_key: str = Form("")):
    t = _team_or_404(token)
    fields: dict = {}
    url = url.strip()
    if url != t["url"]:
        if url:
            try:
                url = check_url(url, allow_private=DEV)
            except UnsafeURL as e:
                return _back(token, err=str(e))
        fields["url"] = url
        fields["steady"] = 0
    if api_key.strip():
        key = api_key.strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,200}", key):
            return _back(token, err="API key must be 8 to 200 characters of letters, digits or ._~+/=-")
        fields["api_key"] = key
    store.update_team(t["id"], **fields)
    note = "Saved."
    if "url" in fields and fields["url"] and fields["url"] != t["approved_url"]:
        note = "Saved. Your instructor has to approve a new URL before Flight Control will call it."
    return _back(token, msg=note)


@app.post("/t/{token}/steady")
def team_steady(token: str, on: int = Form(...)):
    t = _team_or_404(token)
    if on and not store.url_ready(t):
        return _back(token, err="Test 1 starts once your URL is approved and your API key is set.")
    store.update_team(t["id"], steady=1 if on else 0)
    return _back(token, msg="Test 1 is on." if on else "Test 1 is off.")


@app.post("/t/{token}/run")
def team_run(token: str, test: int = Form(...)):
    t = _team_or_404(token)
    if test not in (2, 3, 4, 5):
        return _back(token, err="Pick test 2, 3, 4 or 5.")
    if not store.url_ready(t):
        return _back(token, err="Your URL has to be approved and your API key set first.")
    if store.active_run(t["id"]):
        return _back(token, err="You already have a run queued or running.")
    if store.practice_runs_today(t["id"]) >= store.PRACTICE_RUNS_PER_DAY:
        return _back(token, err=f"You have used your {store.PRACTICE_RUNS_PER_DAY} runs for the last 24 hours.")
    store.queue_run(t["id"], test)
    return _back(token, msg=f"Test {test} queued. It starts as soon as the engine is free.")


@app.get("/api/t/{token}/live")
def team_live(token: str):
    t = _team_or_404(token)
    snap, updated = store.get("live", {})
    mine = next((x for x in (snap or {}).get("teams", []) if x.get("id") == t["id"]), None)
    active = store.active_run(t["id"])
    return {"updated": updated, "team": mine, "active": active and {"id": active["id"], "test": active["test"],
                                                                     "status": active["status"]}}


@app.get("/t/{token}/runs/{run_id}", response_class=HTMLResponse)
def team_report(request: Request, token: str, run_id: int):
    t = _team_or_404(token)
    r = store.run(run_id)
    if not r or r["team_id"] != t["id"]:
        raise HTTPException(404)
    return _page(request, "report.html", run=r, team=t, back=f"/t/{token}")


# -- instructor ----------------------------------------------------------------------

def admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    ok = (creds is not None and ADMIN_PASSWORD
          and hmac.compare_digest(creds.username, "admin")
          and hmac.compare_digest(creds.password, ADMIN_PASSWORD))
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Flight Control admin"'})
    return "admin"


def _csrf() -> str:
    return hmac.new(SECRET.encode(), b"admin-forms", hashlib.sha256).hexdigest()[:32]


def _check_csrf(token: str) -> None:
    if not hmac.compare_digest(token, _csrf()):
        raise HTTPException(403, "bad form token")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, msg: str = "", _: str = Depends(admin)):
    teams = store.teams()
    snap, updated = store.get("live", {})
    live_by_id = {x.get("id"): x for x in (snap or {}).get("teams", [])}
    return _page(request, "admin.html", teams=teams, runs=store.runs_for(None, 60), live=live_by_id,
                 engine_age=time.time() - updated if updated else None, csrf=_csrf(), msg=msg,
                 public_url=os.environ.get("FC_PUBLIC_URL", ""))


def _admin_back(msg: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse("/admin?" + urlencode({"msg": msg}), status_code=303)


@app.post("/admin/teams")
def admin_add_team(name: str = Form(...), csrf: str = Form(...), _: str = Depends(admin)):
    _check_csrf(csrf)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or secrets.token_hex(3)
    if store.team_by_slug(slug):
        slug = f"{slug}-{secrets.token_hex(2)}"
    store.create_team(name.strip()[:60], slug)
    return _admin_back(f"Created {name}.")


@app.post("/admin/teams/{team_id}/approve")
def admin_approve(team_id: int, csrf: str = Form(...), _: str = Depends(admin)):
    _check_csrf(csrf)
    t = store.team_by_id(team_id)
    if not t:
        raise HTTPException(404)
    store.update_team(team_id, approved_url=t["url"])
    return _admin_back(f"Approved {t['url']} for {t['name']}.")


@app.post("/admin/teams/{team_id}/steady")
def admin_steady(team_id: int, on: int = Form(...), csrf: str = Form(...), _: str = Depends(admin)):
    _check_csrf(csrf)
    store.update_team(team_id, steady=1 if on else 0)
    return _admin_back("Updated.")


@app.post("/admin/teams/{team_id}/run")
def admin_run(team_id: int, test: int = Form(...), official: int = Form(0), csrf: str = Form(...),
              _: str = Depends(admin)):
    _check_csrf(csrf)
    if test not in (2, 3, 4, 5):
        raise HTTPException(400)
    store.queue_run(team_id, test, official=bool(official))
    return _admin_back(f"Queued test {test}.")


@app.post("/admin/runs/{run_id}/cancel")
def admin_cancel(run_id: int, csrf: str = Form(...), _: str = Depends(admin)):
    _check_csrf(csrf)
    store.cancel_run(run_id)
    return _admin_back("Cancelled if it had not started.")


@app.get("/admin/runs/{run_id}", response_class=HTMLResponse)
def admin_report(request: Request, run_id: int, _: str = Depends(admin)):
    r = store.run(run_id)
    if not r:
        raise HTTPException(404)
    return _page(request, "report.html", run=r, team=store.team_by_id(r["team_id"]), back="/admin")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
