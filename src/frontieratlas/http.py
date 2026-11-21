"""Bounded async HTTP transport with origin pacing and retry budgets."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from typing import Callable, Iterable, TypeVar
from urllib.parse import urljoin, urlsplit

import aiohttp

from .model import parse_time, public_url

T = TypeVar("T")


class FetchError(Exception):
    def __init__(self, status: int, url: str, detail: str = ""):
        self.status = status
        self.url = url
        self.detail = detail
        super().__init__(f"HTTP {status} at {urlsplit(url).hostname}: {detail[:160]}")


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Disallow private and local addresses from a publisher-supplied URL."""

    def __init__(self):
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        addresses = await self.resolver.resolve(host, port, family)
        public = [item for item in addresses if ipaddress.ip_address(item["host"]).is_global]
        if len(public) != len(addresses) or not public:
            raise OSError("Origin did not resolve exclusively to public addresses")
        return public

    async def close(self):
        await self.resolver.close()


@dataclass
class Origin:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_start: float = 0


@dataclass(frozen=True)
class Response:
    url: str
    body: bytes
    date: datetime | None

    def json(self):
        return json.loads(self.body)


def retry_after(value: str | None, now: datetime) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        parsed = parse_time(value)
        return max(0, (parsed - now).total_seconds()) if parsed else 0


class Http:
    def __init__(self, parallel: int = 24, requests_per_second: float = 7, max_attempts: int = 4):
        self.parallel = parallel
        self.requests_per_second = requests_per_second
        self.max_attempts = max_attempts
        self.origins: dict[str, Origin] = {}

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=self.parallel, limit_per_host=8, resolver=PublicResolver())
        self.session = aiohttp.ClientSession(
            connector=connector,
            trust_env=True,
            headers={"User-Agent": "FrontierAtlasCatalog/1.0", "Accept": "*/*"},
            timeout=aiohttp.ClientTimeout(total=22),
        )
        return self

    async def __aexit__(self, *_exc):
        await self.session.close()

    async def wait_turn(self, url: str) -> Origin:
        host = urlsplit(url).hostname or ""
        origin = self.origins.setdefault(host, Origin())
        interval = 3.1 if host == "export.arxiv.org" else 1 / self.requests_per_second
        loop = asyncio.get_running_loop()
        while True:
            async with origin.lock:
                now = loop.time()
                delay = max(0, origin.next_start - now)
                if not delay:
                    origin.next_start = now + interval
                    return origin
            await asyncio.sleep(delay)

    async def request(
        self, url: str, *, payload: dict | None = None, headers: dict | None = None, limit: int = 8_000_000,
    ) -> Response:
        if not public_url(url):
            raise FetchError(400, url, "Nonpublic or malformed URL")
        current = url
        attempt = 0
        redirects = 0
        while attempt < self.max_attempts:
            origin = await self.wait_turn(current)
            try:
                method = "POST" if payload is not None else "GET"
                async with self.session.request(
                    method, current, headers=headers, json=payload, allow_redirects=False
                ) as reply:
                    if reply.status in {301, 302, 303, 307, 308} and not headers and payload is None and redirects < 5:
                        location = reply.headers.get("Location")
                        target = urljoin(current, location) if location else ""
                        if not public_url(target) or (current.startswith("https:") and not target.startswith("https:")):
                            raise FetchError(400, current, "Unsafe redirect")
                        current = target
                        redirects += 1
                        continue
                    if reply.status >= 400 or reply.status in {301, 302, 303, 307, 308}:
                        excerpt = (await reply.content.read(3072)).decode(errors="replace")
                        # Retain only indicators used to classify provider context errors.
                        context = any(term in excerpt.casefold() for term in ("context length", "context window", "too many tokens", "token limit", "context_length_exceeded", "input is too long"))
                        detail = "context overflow" if context else f"response status {reply.status}"
                        failure = FetchError(reply.status, current, detail)
                        if reply.status not in {408, 425, 429, 500, 502, 503, 504}:
                            raise failure
                        pause = retry_after(reply.headers.get("Retry-After"), datetime.now(UTC))
                        delay = max(pause, min(24, 2 ** attempt) + random.uniform(0, 0.4))
                        if reply.status == 429 or pause:
                            async with origin.lock:
                                origin.next_start = max(origin.next_start, asyncio.get_running_loop().time() + delay)
                        if attempt + 1 == self.max_attempts or delay > 120:
                            raise failure
                    else:
                        content = bytearray()
                        async for block in reply.content.iter_chunked(64 * 1024):
                            content.extend(block)
                            if len(content) > limit:
                                raise FetchError(413, current, "Response exceeds read limit")
                        return Response(str(reply.url), bytes(content), parse_time(reply.headers.get("Date")))
            except (aiohttp.ClientError, TimeoutError, OSError) as error:
                if attempt + 1 == self.max_attempts:
                    raise FetchError(0, current, type(error).__name__) from error
                delay = min(24, 2 ** attempt) + random.uniform(0, 0.4)
            attempt += 1
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")


async def batches(items: Iterable[T], worker: Callable, width: int):
    """Yield settled batches. Limit scheduled work to one batch at a time."""
    iterator = iter(items)
    while batch := list(islice(iterator, width)):
        values = await asyncio.gather(*(worker(item) for item in batch), return_exceptions=True)
        for value in values:
            if isinstance(value, BaseException):
                raise value
            yield value
