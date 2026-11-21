"""Startup, product, and research paper source adapters."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode, urljoin

from lxml import etree, html

from .http import FetchError, Http, batches
from .identity import Resolver, org_key
from .model import clean, domain, parse_time, public_url, record, text_key, utc

LOG = logging.getLogger(__name__)
YC = "https://yc-oss.github.io/api/companies/all.json"
AGI = "https://agipicks.com/data/tools.json"
PWC = "https://paperswithcode.co"
PRICE = {"free": "FREE", "freemium": "FREEMIUM", "paid": "PAID", "contact-sales": "ENTERPRISE"}
ORGANIZATION = {"Organization", "Corporation", "LocalBusiness", "EducationalOrganization"}


def startup_rows(directory: list[dict], limit: int, end: datetime):
    selected = sorted(
        (v for v in directory if isinstance(v.get("launched_at"), int) and parse_time(v["launched_at"]) and parse_time(v["launched_at"]) <= end),
        key=lambda v: (-v["launched_at"], v["id"]),
    )
    emitted: set[str] = set()
    for row in selected:
        raw = row.get("name")
        name = clean(raw)
        url = public_url(row.get("url"))
        key = org_key(name)
        if not key or key in emitted or not url or row.get("status") != "Active" or "AI" not in row.get("tags", []) or row.get("nonprofit"):
            continue
        count = row.get("team_size")
        content = {
            "entityName": name,
            "data": {"employeeCount": count if type(count) is int and count > 0 else None},
            "website": public_url(row.get("website")) or None,
            "batch": row.get("batch"),
            "locations": clean(row.get("all_locations")),
            "description": clean(row.get("one_liner")),
            "industry": row.get("industry"),
            "tags": row.get("tags", []),
            "directoryId": row["id"],
        }
        item = record("STARTUP", "ycombinator/" + str(row["id"]), "Y Combinator", url, content)
        aliases = [(raw, name, "Directory name", url, url)]
        aliases.extend((value, name, "Former name in directory", url, url) for value in row.get("former_names", []) if clean(value) and clean(value) != name)
        yield item, aliases
        emitted.add(key)
        if len(emitted) == limit:
            break


def json_nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from json_nodes(child)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from json_nodes(child)


def publisher_from_html(body: bytes, website: str) -> tuple[str, str] | None:
    doc = html.fromstring(body)
    organizations: set[str] = set()
    for block in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(block.text or "")
        except ValueError:
            continue
        for item in json_nodes(data):
            kinds = item.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if not any(str(kind).rsplit("/", 1)[-1] in ORGANIZATION for kind in kinds):
                continue
            claim = item.get("url") or item.get("@id")
            name = clean(item.get("legalName") or item.get("name"))
            if isinstance(claim, str) and domain(urljoin(website, claim)) == domain(website) and 2 <= len(name) <= 80:
                organizations.add(name)
    if len(organizations) == 1:
        return next(iter(organizations)), website
    return None


def product_candidates(directory: list[dict]):
    names: set[str] = set()
    for row in sorted(directory, key=lambda v: (text_key(v.get("name", "")), v.get("slug", ""))):
        key = text_key(row.get("name", ""))
        if not key or key in names or row.get("pricingModel") not in PRICE or row.get("retired"):
            continue
        if not public_url(row.get("url")) or not public_url(row.get("website")):
            continue
        names.add(key)
        yield row


async def product_row(http: Http, resolver: Resolver, row: dict, inspect_vendor: bool):
    name = clean(row["name"])
    url, website = public_url(row["url"]), public_url(row["website"])
    owner = resolver.resolve(website=website)
    raw_owner = owner.canonical if owner else ""
    owner_source = website
    if inspect_vendor and not owner:
        try:
            response = await http.request(website, limit=850_000)
            if domain(response.url) == domain(website):
                if discovered := publisher_from_html(response.body, response.url):
                    raw_owner, owner_source = discovered
                    owner = resolver.resolve(raw_owner, website, owner_source)
        except (FetchError, ValueError, etree.ParserError):
            LOG.debug("Vendor metadata unavailable at %s", domain(website))
    content = {
        "productName": name,
        "startupName": owner.canonical if owner else None,
        "pricingModel": PRICE[row["pricingModel"]],
        "sourcePricingModel": row["pricingModel"],
        "pricingCheckedDate": row.get("pricesCheckedAt"),
        "website": website,
        "category": row.get("category"),
        "publisherEvidence": {"url": owner.evidence, "rule": owner.rule} if owner else None,
        "license": "https://creativecommons.org/licenses/by/4.0/",
    }
    item = record("PRODUCT", "agipicks/" + str(row["slug"]), "AGIPicks", url, content)
    aliases = [(row["name"], name, "Directory name", url, url)]
    if owner:
        aliases.append((raw_owner, owner.canonical, owner.rule, owner_source, owner.evidence))
    return item, aliases


async def paper_pages(http: Http):
    seen_pages = set()
    page = 1
    while page and page not in seen_pages:
        seen_pages.add(page)
        url = f"{PWC}/api/v1/papers?" + urlencode({"page_size": 500, "has_code": "true", "page": page})
        payload = (await http.request(url)).json()
        yield payload["results"]
        page = payload.get("next_page")
    if page:
        raise ValueError("Papers with Code returned a repeated page cursor")


def paper_candidate(paper: dict, end: datetime):
    arxiv = paper.get("arxiv_id")
    title = clean(paper.get("title"))
    if not arxiv or not title or not paper.get("authors"):
        return False
    try:
        return date.fromisoformat(paper["published"]) <= end.date()
    except (TypeError, ValueError, KeyError):
        return False


async def paper_row(http: Http, paper: dict, max_star_age: timedelta) -> dict | None:
    source_url = f"{PWC}/paper/{paper['id']}"
    metric_url = f"{PWC}/api/v1/papers/{paper['id']}/repositories"
    try:
        metrics = (await http.request(metric_url)).json()
    except (FetchError, ValueError):
        LOG.warning("Repository lookup unavailable for paper %s", paper["id"])
        return None
    now = datetime.now(UTC)
    candidates = []
    for repo in metrics:
        link = public_url(repo.get("url"))
        stars = repo.get("num_stars")
        observed = parse_time(repo.get("trending_score_updated_at"))
        if domain(link) == "github.com" and type(stars) is int and stars >= 0 and observed and now - max_star_age <= observed <= now:
            candidates.append((repo, link, observed))
    if not candidates:
        return None
    repo, link, observed = min(candidates, key=lambda v: (not v[0].get("is_official"), -v[0]["num_stars"], v[1]))
    content = {
        "title": clean(paper["title"]),
        "authors": [clean(author) for author in paper["authors"] if clean(author)],
        "paper_url": f"https://arxiv.org/abs/{paper['arxiv_id']}",
        "github_url": link,
        "github_stars": repo["num_stars"],
        "published_date": paper["published"],
        "publicationPrecision": "calendar-day",
        "githubStarsUpdatedAt": utc(observed),
        "githubMetricsSourceUrl": metric_url,
        "githubOfficial": bool(repo.get("is_official")),
        "arxivId": paper["arxiv_id"],
    }
    return record("RESEARCH_PAPER", paper["arxiv_id"].casefold(), "Papers with Code", source_url, content)


async def arxiv_timestamps(http: Http, paper_ids: list[str]) -> dict[str, str]:
    """Decorate day precision with the original arXiv publication timestamp."""
    result: dict[str, str] = {}
    for offset in range(0, len(paper_ids), 75):
        group = paper_ids[offset : offset + 75]
        query = urlencode({"id_list": ",".join(group), "max_results": len(group)})
        try:
            raw = await http.request(f"https://export.arxiv.org/api/query?{query}", limit=2_000_000)
            tree = etree.fromstring(raw.body, etree.XMLParser(resolve_entities=False, no_network=True))
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for item in tree.xpath("./a:entry", namespaces=ns):
                url = item.findtext("a:id", namespaces=ns) or ""
                key = url.split("/abs/", 1)[-1].rsplit("v", 1)[0]
                if key in group and (date_value := parse_time(item.findtext("a:published", namespaces=ns))):
                    result[key] = utc(date_value)
        except (FetchError, etree.XMLSyntaxError):
            LOG.warning("arXiv timestamp batch unavailable at offset %s", offset)
    return result


async def paper_rows(http: Http, limit: int, end: datetime, max_star_age: timedelta):
    paper_ids, titles = set(), set()
    emitted = 0

    async def fetch(paper):
        return await paper_row(http, paper, max_star_age)

    async for page in paper_pages(http):
        ready = (p for p in page if paper_candidate(p, end) and p["arxiv_id"] not in paper_ids)
        async for item in batches(ready, fetch, min(12, http.parallel)):
            if not item:
                continue
            content = item["content"]
            title = text_key(content["title"])
            if not title or title in titles or content["arxivId"] in paper_ids:
                continue
            titles.add(title)
            paper_ids.add(content["arxivId"])
            emitted += 1
            yield item
            if emitted == limit:
                return
