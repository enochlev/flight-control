"""Flight Control: the traffic generator and grader for COMP 4340/6340 Projects 2 and 3."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time


def _cmd_test(args: argparse.Namespace) -> int:
    from .client import TeamClient, check_url
    from .grading import run_test
    from .workload import Recorder

    base = check_url(args.url, allow_private=True)
    t0 = time.time()
    quiet = args.json  # with --json, print nothing but the JSON

    def say(text: str) -> None:
        if not quiet:
            print(text, flush=True)

    def emit(kind: str, **d) -> None:
        if kind in ("phase", "run"):
            say(f"[{time.time() - t0:6.1f}s] {d.get('text', '')}")

    async def go() -> dict:
        def show(method: str, path: str, r) -> None:
            status = r.status or r.error
            say(f"  {method:6s} {path[:70]:70s} {status!s:>8}  {r.rtt_ms:7.1f} ms")

        client = TeamClient(base, args.key, on_call=show if args.verbose and not quiet else None)
        rec = Recorder()
        stop_at = time.time() + args.minutes * 60 if args.test == 1 else None

        async def ticker():
            while True:
                await asyncio.sleep(10)
                w = rec.window(10)
                v = sum(rec.violations.values())
                say(f"[{time.time() - t0:6.1f}s] {w['rate']:.1f} travelers/s, {w['bad']} not ok in the last 10 s, "
                    f"{v} problems so far")

        tick = asyncio.create_task(ticker())
        try:
            try:
                return await asyncio.wait_for(
                    run_test(args.test, client, seed=args.seed, run_tag=f"cli{int(t0)}", emit=emit, rec=rec,
                             stop=(lambda: time.time() > stop_at) if stop_at else (lambda: False)),
                    timeout=max(15 * 60, args.minutes * 60 + 120))
            except asyncio.TimeoutError:
                return {"score": 0.0, "gates_ok": False, "gates": ["the test did not finish within 15 minutes"],
                        "summary": "stopped after 15 minutes"}
        finally:
            tick.cancel()
            await client.close()

    res = asyncio.run(go())
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("gates_ok") in (True, None) else 1
    print()
    print(f"Test {args.test}: {res.get('summary')}")
    if res.get("score") is not None:
        print(f"Score {res['score']:.2f} out of 1.00. Rules {'kept' if res.get('gates_ok') else 'BROKEN'}.")
    trav, ok = res.get("travelers", 0), res.get("travelers_ok", 0)
    print(f"{ok} of {trav} travelers correct.")
    for g in res.get("gates", []):
        print(f"  FAILED: {g}")
    for s in res.get("steps", []):
        print(f"  step {s['rate']:>2}/s: {s['correct']:.0%} correct, p95 server {s['p95_server_ms']} ms, "
              f"{'passed' if s['passed'] else 'not passed'}")
    for r in res.get("rounds", []):
        if not r.get("passed"):
            print(f"  round {r['flight']} with {r['left']} seats left: {r.get('why')}")
    if res.get("timeline"):
        print(f"  restart: {res['timeline']}")
    for m in res.get("mismatches", [])[:15]:
        print(f"  mismatch: {m}")
    if res.get("failed_because"):
        print("Why travelers failed:")
        for k, n in res["failed_because"].items():
            print(f"  {n:5d} x {k}")
    if res.get("violations"):
        print("Rules broken (with examples):")
        for k, v in res["violations"].items():
            print(f"  {v['count']:5d} x {k}")
            for ex in v["examples"][:2]:
                print(f"          {ex}")
    print("Per endpoint:")
    for op, s in res.get("ops", {}).items():
        print(f"  {op:12s} n={s['n']:6d}  p95 round trip {s['p95_rtt_ms']} ms   p95 server {s['p95_server_ms']} ms   {s['codes']}")
    return 0 if res.get("gates_ok") in (True, None) else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="flightcontrol")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("web", help="serve the Flight Control web site")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    sub.add_parser("engine", help="run the traffic engine")
    t = sub.add_parser("test", help="run one test against your own airline")
    t.add_argument("--url", required=True, help="for example http://localhost:8080")
    t.add_argument("--key", required=True, help="your API key")
    t.add_argument("--test", type=int, choices=[1, 2, 3, 4, 5], required=True)
    t.add_argument("--seed", type=int, default=4340)
    t.add_argument("--minutes", type=float, default=2.0, help="how long test 1 runs")
    t.add_argument("--json", action="store_true", help="print the full result as JSON")
    t.add_argument("--verbose", "-v", action="store_true", help="print every call Flight Control makes")
    c = sub.add_parser("catalog", help="write the flight catalog CSV files")
    c.add_argument("--out", default=".")
    a = sub.add_parser("add-team", help="create a team and print its private link")
    a.add_argument("name")
    a.add_argument("slug")
    args = p.parse_args()

    if args.cmd == "web":
        import uvicorn
        uvicorn.run("flightcontrol.web:app", host=args.host, port=args.port, proxy_headers=True,
                    forwarded_allow_ips="*")
    elif args.cmd == "engine":
        from .engine import run
        run()
    elif args.cmd == "test":
        sys.exit(_cmd_test(args))
    elif args.cmd == "catalog":
        from pathlib import Path

        from . import catalog
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "flights.csv").write_bytes(catalog.get().to_csv().encode())  # same bytes on every OS
        (out / "airports.csv").write_bytes(catalog.airports_csv().encode())
        import hashlib
        data = (out / "flights.csv").read_bytes()
        print(f"wrote {out / 'flights.csv'} ({len(catalog.get().flights):,} flights) and {out / 'airports.csv'}")
        print(f"sha256 {hashlib.sha256(data).hexdigest()}")
    elif args.cmd == "add-team":
        from . import store
        store.init()
        t = store.create_team(args.name, args.slug)
        print(f"team {t['name']}: /t/{t['token']}")
