"""Command-line entry point for catalog and signal intake."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .catalogs import AGI, YC, arxiv_timestamps, paper_rows, product_candidates, product_row, startup_rows
from .extraction import FIELDS, attach_extraction, configured_providers
from .http import FetchError, Http, batches
from .identity import Resolver
from .model import parse_time
from .signals import scan_signals
from .store import Store

LOG = logging.getLogger(__name__)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def arguments():
    parser = argparse.ArgumentParser(prog="frontieratlas")
    parser.add_argument("--mode", choices=["all", "bulk", "signals"], default="all")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--cutoff", help="ISO 8601 timestamp with time zone. Defaults to the start of the run.")
    parser.add_argument("--startups", type=positive, default=1100)
    parser.add_argument("--products", type=positive, default=1150)
    parser.add_argument("--papers", type=positive, default=1050)
    parser.add_argument("--parallel", type=positive, default=24)
    parser.add_argument("--paper-star-hours", type=positive, default=24)
    parser.add_argument("--enrich-vendors", action="store_true")
    parser.add_argument("--arxiv-timestamps", action="store_true")
    parser.add_argument("--provider-records-per-type", type=positive,
                        help="Limit extraction requests to this many bulk records per record type")
    parser.add_argument("--require-targets", action="store_true")
    args = parser.parse_args()
    if args.cutoff and not parse_time(args.cutoff):
        parser.error("--cutoff must include a valid time zone")
    if args.cutoff and parse_time(args.cutoff) > datetime.now(UTC):
        parser.error("--cutoff must be at or before the current time")
    return args


async def collect(args) -> tuple[dict, bool]:
    end = parse_time(args.cutoff) or datetime.now(UTC)
    end = end.replace(microsecond=0)
    store = Store(args.db or args.out / "frontieratlas.sqlite3")
    coverage = []
    collected = {"STARTUP": 0, "PRODUCT": 0, "RESEARCH_PAPER": 0}
    try:
        async with Http(parallel=args.parallel) as http:
            providers = configured_providers()

            async def write_catalog_record(item, aliases=()):
                kind = item["recordType"]
                if providers and (args.provider_records_per_type is None or collected[kind] < args.provider_records_per_type):
                    content = item["content"]
                    fields = sorted(FIELDS[kind])
                    text = "\n".join(f"{field}: {content[field]}" for field in fields if content.get(field))
                    await attach_extraction(http, providers, item, text)
                store.put(item, aliases)
                collected[kind] += 1

            try:
                startups = (await http.request(YC, limit=16_000_000)).json()
                if not isinstance(startups, list):
                    raise ValueError("Company directory did not return a list")
            except (FetchError, ValueError) as error:
                LOG.error("Company directory unavailable: %s", error)
                startups = []
            resolver = Resolver(startups)
            if args.mode in {"all", "signals"}:
                coverage = await scan_signals(http, store, end, resolver, providers)
            if args.mode in {"all", "bulk"}:
                for item, aliases in startup_rows(startups, args.startups, end):
                    await write_catalog_record(item, aliases)
                try:
                    products = (await http.request(AGI, limit=8_000_000)).json()["tools"]
                    async def fetch_product(row):
                        return await product_row(http, resolver, row, args.enrich_vendors)
                    subset = itertools.islice(product_candidates(products), args.products)
                    async for item, aliases in batches(subset, fetch_product, args.parallel):
                        await write_catalog_record(item, aliases)
                except (FetchError, ValueError, KeyError, TypeError) as error:
                    LOG.error("Product directory ended with %s", error)

                async def write_batch(batch):
                    ids = [item["content"]["arxivId"] for item in batch]
                    timestamps = await arxiv_timestamps(http, ids) if args.arxiv_timestamps else {}
                    for item in batch:
                        content = item["content"]
                        if timestamp := timestamps.get(content["arxivId"]):
                            if parse_time(timestamp) <= end:
                                content.update(published_date=timestamp, publicationPrecision="timestamp")
                        await write_catalog_record(item)

                page = []
                try:
                    async for item in paper_rows(http, args.papers, end, timedelta(hours=args.paper_star_hours)):
                        page.append(item)
                        if len(page) >= 75:
                            await write_batch(page)
                            page = []
                except (FetchError, ValueError, KeyError, TypeError) as error:
                    LOG.error("Research catalog ended with %s", error)
                if page:
                    await write_batch(page)

        manifest = store.export(args.out, end, coverage)
        targets = {"STARTUP": args.startups, "PRODUCT": args.products, "RESEARCH_PAPER": args.papers}
        missing = args.mode != "signals" and any(collected[kind] < minimum for kind, minimum in targets.items())
        manifest["bulkRecordsObservedThisRun"] = collected
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest, bool(missing)
    finally:
        store.close()


def main():
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manifest, shortfall = asyncio.run(collect(args))
    print(json.dumps(manifest["counts"], indent=2))
    if args.require_targets and shortfall:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
