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

    def emit(kind: str, **d) -> None:
        if kind in ("phase", "run"):
            print(f"[{time.time() - t0:6.1f}s] {d.get('text', '')}", flush=True)

    async def go() -> dict:
        def show(method: str, path: str, r) -> None:
            status = r.status or r.error
            print(f"  {method:6s} {path[:70]:70s} {status!s:>8}  {r.rtt_ms:7.1f} ms", flush=True)

        client = TeamClient(base, args.key, on_call=show if args.verbose else None)
        rec = Recorder()
        stop_at = time.time() + args.minutes * 60 if args.test == 1 else None

        async def ticker():
            while True:
                await asyncio.sleep(10)
                w = rec.window(10)
                v = sum(rec.violations.values())
                print(f"[{time.time() - t0:6.1f}s] {w['rate']:.1f} travelers/s, {w['bad']} not ok in the last 10 s, "
                      f"{v} violations so far", flush=True)

        tick = asyncio.create_task(ticker())
        try:
            return await run_test(args.test, client, seed=args.seed, run_tag=f"cli{int(t0)}", emit=emit,
                                  rec=rec, stop=(lambda: time.time() > stop_at) if stop_at else (lambda: False))
        finally:
            tick.cancel()
            await client.close()

    res = asyncio.run(go())
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print()
        print(f"Test {args.test}: {res.get('summary')}")
        print(f"Score {res.get('score')}   gates passed: {res.get('gates_ok')}")
        for g in res.get("gates", []):
            print(f"  gate failed: {g}")
        for k, v in res.get("violations", {}).items():
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
        (out / "flights.csv").write_text(catalog.get().to_csv(), encoding="utf-8")
        (out / "airports.csv").write_text(catalog.airports_csv(), encoding="utf-8")
        print(f"wrote {out / 'flights.csv'} and {out / 'airports.csv'}")
    elif args.cmd == "add-team":
        from . import store
        store.init()
        t = store.create_team(args.name, args.slug)
        print(f"team {t['name']}: /t/{t['token']}")
