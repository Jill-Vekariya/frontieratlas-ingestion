"""Canonical records, publication bounds, and stable public identifiers."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}
KINDS = ("STARTUP", "PRODUCT", "RESEARCH_PAPER", "JOB", "NEWS")
SPACE = re.compile(r"\s+")
RELATIVE = re.compile(r"^(\d+)\s*(min(?:ute)?s?|hr?s?|hours?|days?)\s+ago$", re.I)


def clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    return SPACE.sub(" ", normalized).strip()


def text_key(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", clean(value).casefold()).split())


def utc(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def public_url(value: object) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return ""
    try:
        url = urlsplit(value)
        host = url.hostname
        if url.scheme not in {"http", "https"} or not host or url.username or url.password or url.port not in {80, 443, None}:
            return ""
        if not host.isascii() or host.endswith((".local", ".internal", ".localhost", ".test")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host, re.I) or not re.search(r"[a-z]", host, re.I):
                return ""
        params = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if not k.startswith("utm_") and k not in TRACKING]
        path = url.path.rstrip("/") if url.path != "/" else ""
        return urlunsplit((url.scheme, url.netloc.lower(), path or "", urlencode(sorted(params)), ""))
    except ValueError:
        return ""


def domain(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def identifier(kind: str, value: str) -> str:
    return f"{kind.lower()}:{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def url_key(value: str) -> str:
    return value.split("://", 1)[-1].removeprefix("www.")


def record(kind: str, key: str, label: str, url: str, content: dict, collected_at: str | None = None) -> dict:
    if kind not in KINDS or not (safe := public_url(url)):
        raise ValueError(f"Invalid record source for {kind}")
    return {
        "id": identifier(kind, key),
        "schemaVersion": "1.0",
        "recordType": kind,
        "source": {"name": label, "url": safe},
        "content": content,
        "collectedAt": collected_at or utc(),
    }


def parse_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


@dataclass(frozen=True)
class Publication:
    earliest: datetime
    latest: datetime
    precision: str

    def within(self, end: datetime) -> bool:
        end = end.astimezone(UTC)
        return end - timedelta(hours=24) <= self.earliest <= self.latest <= end


def publication(value: object, anchor: datetime | None = None, date_in_utc: bool = False) -> Publication | None:
    if exact := parse_time(value):
        return Publication(exact, exact, "timestamp")
    if not isinstance(value, str):
        return None
    if match := RELATIVE.fullmatch(clean(value)):
        if anchor is None or anchor.tzinfo is None:
            return None
        amount, unit = match.groups()
        factor = 60 if unit.lower().startswith("min") else 86_400 if unit.lower().startswith("day") else 3_600
        anchor = anchor.astimezone(UTC)
        return Publication(
            anchor - timedelta(seconds=(int(amount) + 1) * factor),
            anchor - timedelta(seconds=int(amount) * factor),
            "relative-interval",
        )
    if date_in_utc and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            start = datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
        except ValueError:
            return None
        return Publication(start, start + timedelta(days=1, microseconds=-1), "calendar-day-utc")
    return None
