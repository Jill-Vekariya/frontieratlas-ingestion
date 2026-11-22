"""Durable records, source evidence, publication checks, and export."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .model import EMPLOYER_JOB_DATE, Publication, utc

NAMES = {
    "STARTUP": "Startups",
    "PRODUCT": "Products",
    "RESEARCH_PAPER": "Research Papers",
    "JOB": "Jobs",
    "NEWS": "News",
}
BASE_CONTENT_FIELDS = {
    "STARTUP": ("entityName", "data.employeeCount"),
    "PRODUCT": ("productName", "startupName", "pricingModel"),
    "RESEARCH_PAPER": ("title", "authors", "paper_url", "github_url", "github_stars", "published_date"),
    "JOB": ("title", "company", "date", "is_remote", "role_family"),
    "NEWS": ("title", "date"),
}
SIGNAL_WINDOW = """
  published_min >= ? AND published_max <= ?
  AND (kind <> 'JOB' OR (
    json_extract(payload, '$.content.dateEvidence.source') = ?
    AND json_extract(payload, '$.content.dateEvidence.url') LIKE 'https://boards-api.greenhouse.io/%'
    AND json_extract(payload, '$.content.dateEvidence.precision') = 'timestamp'
  ))
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  published_min TEXT, published_max TEXT
);
CREATE INDEX IF NOT EXISTS record_window ON record(kind, published_min, published_max);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY REFERENCES record(id), body_text TEXT NOT NULL, body_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping (
  record_id TEXT NOT NULL REFERENCES record(id), raw TEXT NOT NULL, canonical TEXT NOT NULL,
  rule TEXT NOT NULL, source TEXT NOT NULL, canonical_source TEXT NOT NULL, collected TEXT NOT NULL,
  PRIMARY KEY (record_id, raw, canonical, rule, source, canonical_source)
);
CREATE TABLE IF NOT EXISTS observation (
  id TEXT PRIMARY KEY, source_url TEXT NOT NULL, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim (
  id TEXT PRIMARY KEY, lease_until TEXT NOT NULL, owner TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_observation (
  paper_id TEXT NOT NULL REFERENCES record(id), repository_url TEXT NOT NULL,
  observed_at TEXT NOT NULL, stars INTEGER NOT NULL, evidence_url TEXT NOT NULL,
  PRIMARY KEY(paper_id, repository_url, observed_at)
);
"""


def json_line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def exact_string(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def flatten(data: dict, prefix: str = "") -> dict:
    result = {}
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, name))
        else:
            result[name] = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
    return result


def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class Store:
    def __init__(self, path: Path | str):
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def claim_signal(self, row_id: str, owner: str, at: datetime) -> bool:
        now = exact_string(at)
        with self.db:
            statement = """
            INSERT INTO claim(id, lease_until, owner)
            SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM record WHERE id = ?)
            ON CONFLICT(id) DO UPDATE SET lease_until=excluded.lease_until, owner=excluded.owner
            WHERE claim.lease_until <= ?
            """
            params = (row_id, exact_string(at + timedelta(minutes=10)), owner, row_id, now)
            return self.db.execute(statement, params).rowcount == 1

    def release(self, row_id: str, owner: str):
        with self.db:
            self.db.execute("DELETE FROM claim WHERE id=? AND owner=?", (row_id, owner))

    def observe(self, row_id: str, url: str, reason: str):
        stamp = utc()
        with self.db:
            self.db.execute("""
            INSERT INTO observation VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, reason=excluded.reason
            """, (row_id, url, stamp, stamp, reason))

    def put(self, item: dict, aliases=(), publication: Publication | None = None, body: str = "", owner: str | None = None):
        stamp, row_id, kind = item["collectedAt"], item["id"], item["recordType"]
        lower, upper = (exact_string(publication.earliest), exact_string(publication.latest)) if publication else (None, None)
        with self.db:
            if owner:
                claim = self.db.execute("SELECT owner FROM claim WHERE id=?", (row_id,)).fetchone()
                if not claim or claim[0] != owner:
                    return False
            if kind == "RESEARCH_PAPER":
                prior = self.db.execute("SELECT payload FROM record WHERE id=?", (row_id,)).fetchone()
                if prior and json.loads(prior[0])["content"]["githubStarsUpdatedAt"] > item["content"]["githubStarsUpdatedAt"]:
                    return False
            self.db.execute("""
            INSERT INTO record VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, last_seen=excluded.last_seen,
              published_min=excluded.published_min, published_max=excluded.published_max
            """, (row_id, kind, json_line(item), stamp, stamp, lower, upper))
            if body:
                digest = hashlib.sha256(body.encode()).hexdigest()
                self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?, ?)", (row_id, body, digest))
            self.db.executemany(
                "INSERT OR IGNORE INTO mapping VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(row_id, *alias, stamp) for alias in aliases],
            )
            if kind == "RESEARCH_PAPER":
                content = item["content"]
                self.db.execute("INSERT OR IGNORE INTO metric_observation VALUES (?, ?, ?, ?, ?)", (
                    row_id, content["github_url"], content["githubStarsUpdatedAt"], content["github_stars"], content["githubMetricsSourceUrl"],
                ))
            if owner:
                self.db.execute("DELETE FROM claim WHERE id=? AND owner=?", (row_id, owner))
            return True

    def export(self, destination: Path, end: datetime, coverage: list[dict]) -> dict:
        destination.mkdir(parents=True, exist_ok=True)
        lower, upper = (exact_string(end - timedelta(hours=24)), exact_string(end))
        window = (lower, upper, EMPLOYER_JOB_DATE)
        totals = {}
        with (destination / "records.jsonl").open("w", newline="\n", encoding="utf-8") as canonical:
            for kind, filename in NAMES.items():
                statement = "SELECT payload FROM record WHERE kind=?"
                params = (kind,)
                if kind in {"JOB", "NEWS"}:
                    statement += f" AND ({SIGNAL_WINDOW})"
                    params += window
                statement += " ORDER BY COALESCE(published_min, first_seen) DESC, id"
                columns = dict.fromkeys([
                    "id", "schemaVersion", "recordType", "source.name", "source.url",
                    *(f"content.{field}" for field in BASE_CONTENT_FIELDS[kind]), "collectedAt",
                ])
                for (payload,) in self.db.execute(statement, params):
                    columns.update(dict.fromkeys(flatten(json.loads(payload))))
                with (destination / f"{filename}.csv").open("w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.DictWriter(output, fieldnames=list(columns))
                    writer.writeheader()
                    count = 0
                    for (payload,) in self.db.execute(statement, params):
                        canonical.write(payload + "\n")
                        writer.writerow({key: csv_value(value) for key, value in flatten(json.loads(payload)).items()})
                        count += 1
                totals[kind] = count
        with (destination / "Entity Mapping Log.csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(["recordType", "source.id", "rawName", "canonicalName", "matchRule", "source.url", "canonicalEvidence.url", "collectedAt"])
            rows = self.db.execute(f"""
              SELECT record.kind, m.* FROM mapping m JOIN record ON m.record_id=record.id
              WHERE record.kind NOT IN ('JOB', 'NEWS') OR ({SIGNAL_WINDOW})
              ORDER BY m.raw=m.canonical, record.kind, m.canonical, m.raw, m.record_id
              """, window)
            for row in rows:
                writer.writerow([csv_value(value) for value in row])
        manifest = {"cutoffUtc": utc(end), "windowStartUtc": utc(end - timedelta(hours=24)), "counts": totals, "feedCoverage": coverage}
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest
