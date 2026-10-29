"""Outbound HTTP to a team's airline, with the guard rails a public site needs.

Flight Control sends traffic to URLs that students typed in, so this module is
where SSRF is stopped: https only, an allowlist of host suffixes, the exact URL
approved by the instructor (checked in store.url_ready), every resolved address
public, no redirects, short timeouts and a cap on response size.
"""

from __future__ import annotations

import asyncio
import ipaddress
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
                 timeout: float = TIMEOUT_S, on_call=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.on_call = on_call  # called with (method, path, Resp) after every request
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout, connect=timeout),
            limits=httpx.Limits(max_connections=max_connections,
                                max_keepalive_connections=max_connections),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, *, json: Any = None,
                   params: dict | None = None, timeout: float | None = None) -> Resp:
        r = await self._call(method, path, json=json, params=params, timeout=timeout)
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
                    params: dict | None = None, timeout: float | None = None) -> Resp:
        t0 = time.perf_counter()
        try:
            kw: dict[str, Any] = {"json": json, "params": params}
            if timeout is not None:
                kw["timeout"] = timeout
            r = await self._client.request(method, path, **kw)
        except httpx.TimeoutException:
            return Resp(0, None, (time.perf_counter() - t0) * 1000, None, "timeout")
        except (httpx.HTTPError, OSError) as e:
            return Resp(0, None, (time.perf_counter() - t0) * 1000, None, f"connect: {type(e).__name__}")
        rtt = (time.perf_counter() - t0) * 1000
        server_ms = None
        m = _TIMING.search(r.headers.get("server-timing", ""))
        if m:
            try:
                server_ms = float(m.group(1))
            except ValueError:
                server_ms = None
        if len(r.content) > MAX_BODY:
            return Resp(r.status_code, None, rtt, server_ms, "body too large")
        body: Any = None
        if r.content:
            try:
                body = r.json()
            except ValueError:
                return Resp(r.status_code, None, rtt, server_ms, "json")
        return Resp(r.status_code, body, rtt, server_ms)


async def gather_limited(coros, limit: int):
    sem = asyncio.Semaphore(limit)

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros))
