"""Outbound HTTP to a team's airline, with the guard rails a public site needs.

Flight Control sends traffic to URLs that students typed in, so this module is
where SSRF is stopped: https only, an allowlist of host suffixes, the exact URL
approved by the instructor (checked in store.url_ready), every resolved address
public, no redirects, short timeouts and a cap on response size.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json as _json
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

TIMEOUT_S = 2.0
MAX_BODY = 1_000_000
USER_AGENT = "FlightControl/1.0 (+NNU COMP 4340)"
_TIMING = re.compile(r"dur=([0-9.]+)")


class UnsafeURL(ValueError):
    pass


def allowed_suffixes() -> list[str]:
    raw = os.environ.get("FC_ALLOWED_HOST_SUFFIXES", "trycloudflare.com,enochlev.com")
    return [s.strip().lower().lstrip(".") for s in raw.split(",") if s.strip()]


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Return the normalized base URL or raise UnsafeURL."""
    parts = urlsplit(url.strip())
    if allow_private:
        if parts.scheme not in ("http", "https"):
            raise UnsafeURL("URL must start with http:// or https://")
    elif parts.scheme != "https":
        raise UnsafeURL("URL must start with https://")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURL("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURL("URL may not contain a user name or password")
    if not allow_private:
        if not any(host == s or host.endswith("." + s) for s in allowed_suffixes()):
            raise UnsafeURL(f"host must end in one of: {', '.join(allowed_suffixes())}")
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise UnsafeURL(f"cannot resolve {host}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise UnsafeURL(f"{host} resolves to a non-public address")
    path = parts.path.rstrip("/")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{path}"


@dataclass(slots=True)
class Resp:
    status: int            # 0 means no HTTP answer (timeout or connection error)
    body: Any
    rtt_ms: float
    server_ms: float | None
    error: str | None = None   # "timeout", "connect", "body", "json"

    @property
    def unknown(self) -> bool:
        return self.status == 0


class TeamClient:
    def __init__(self, base_url: str, api_key: str, *, max_connections: int = 64,
                 timeout: float = TIMEOUT_S, on_call=None, transport=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.on_call = on_call  # called with (method, path, Resp) after every request
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout, connect=timeout),
            limits=httpx.Limits(max_connections=max_connections,
                                max_keepalive_connections=max_connections),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, *, json: Any = None,
                   params: dict | None = None, timeout: float | None = None) -> Resp:
        limit = timeout if timeout is not None else self.timeout
        t0 = time.perf_counter()
        try:
            # One deadline for the whole call, so a server that trickles bytes cannot hold it open.
            r = await asyncio.wait_for(self._call(method, path, json=json, params=params, timeout=limit),
                                       timeout=limit)
        except asyncio.TimeoutError:
            r = Resp(0, None, (time.perf_counter() - t0) * 1000, None, "timeout")
        if self.on_call is not None:
            shown = path
            if params:
                shown += "?" + "&".join(f"{k}={v}" for k, v in params.items())
            try:
                self.on_call(method, shown, r)
            except Exception:
                pass
        return r

    async def _call(self, method: str, path: str, *, json: Any = None,
                    params: dict | None = None, timeout: float) -> Resp:
        t0 = time.perf_counter()
        try:
            async with self._client.stream(method, path, json=json, params=params, timeout=timeout) as r:
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        return Resp(r.status_code, None, (time.perf_counter() - t0) * 1000, None,
                                    "body too large")
                    chunks.append(chunk)
                content = b"".join(chunks)
                headers = r.headers
                status = r.status_code
        except httpx.TimeoutException:
            return Resp(0, None, (time.perf_counter() - t0) * 1000, None, "timeout")
        except (httpx.HTTPError, OSError) as e:
            return Resp(0, None, (time.perf_counter() - t0) * 1000, None, f"connect: {type(e).__name__}")
        rtt = (time.perf_counter() - t0) * 1000
        server_ms = None
        m = _TIMING.search(headers.get("server-timing", ""))
        if m:
            try:
                server_ms = float(m.group(1))
            except ValueError:
                server_ms = None
        if server_ms is not None and (server_ms < 0 or server_ms > rtt):
            # A server cannot spend longer on a request than the request took. Use the round trip.
            server_ms = None
        body: Any = None
        if content:
            try:
                body = _json.loads(content)
            except ValueError:
                return Resp(status, None, rtt, server_ms, "json")
        return Resp(status, body, rtt, server_ms)


async def gather_limited(coros, limit: int):
    sem = asyncio.Semaphore(limit)

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros))
