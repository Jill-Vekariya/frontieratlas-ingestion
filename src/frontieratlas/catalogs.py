"""Startup, product, and research paper source adapters."""

from __future__ import annotations

import json
import logging
import re
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
MARKETPLACE = "https://marketplace.visualstudio.com"
PRICE = {"free": "FREE", "freemium": "FREEMIUM", "paid": "PAID", "contact-sales": "ENTERPRISE"}
ORGANIZATION = {"Organization", "Corporation", "LocalBusiness", "EducationalOrganization"}
PUBLISHER_RELATION = re.compile(
    r"(?i:operated by|owned by|provided by|offered by|published by|agreement between you and)\s+"
    r"([\w &.,’'-]{2,80}?\s+(?:Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|PBC|PLC|B\.V\.))"
    r"(?=\s*[,.;(]|\s*$)"
)
MARKETPLACE_CATEGORIES = ("AI", "Machine Learning", "Chat")
MARKETPLACE_TOPIC = re.compile(
    r"\b(?:AI|LLMs?|GPT|Copilot|Claude|machine learning|deep learning|language models?|neural networks?)\b",
    re.I,
)


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
    organizations: dict[int, set[str]] = {0: set(), 1: set()}
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
                organizations[int(not item.get("legalName"))].add(name)
    candidates = organizations[0] or organizations[1]
    if len(candidates) == 1:
        return next(iter(candidates)), website
    return None


def bears_product_name(candidate: str, product_name: str) -> bool:
    name = "".join(text_key(candidate).split())
    product_key = "".join(text_key(product_name).split())
    return len(product_key) >= 3 and product_key in name


def product_copyright(doc, product_name: str) -> str | None:
    candidates = set()
    for footer in doc.xpath('//footer|//*[@role="contentinfo"]'):
        for raw in footer.xpath(".//text()"):
            line = clean(raw)
            match = re.search(r"(?:©|\bcopyright\b)\s*(?:©\s*)?(?:\d{4}\s*)?(.{2,80})$", line, re.I)
            if not match:
                continue
            name = re.split(r"all rights reserved|[|•·]", match[1], maxsplit=1, flags=re.I)[0].strip(" .,-–")
            if bears_product_name(name, product_name):
                candidates.add(name)
    return next(iter(candidates)) if len(candidates) == 1 else None


async def publisher_evidence(http: Http, website: str, product_name: str):
    page = await http.request(website, limit=3_800_000)
    if domain(page.url) != domain(website):
        return None
    if evidence := publisher_from_html(page.body, page.url):
        return evidence
    doc = html.fromstring(page.body)
    if name := product_copyright(doc, product_name):
        return name, page.url

    policies = []
    for link in doc.xpath("//a[@href]"):
        label = clean(link.text_content())
        if not re.fullmatch(r"terms(?: of (?:service|use))?|privacy(?: policy| notice)?|legal notice", label, re.I):
            continue
        url = public_url(urljoin(page.url, link.get("href")))
        if url and domain(url) == domain(page.url) and url not in policies:
            policies.append(url)
    operators = {}
    for url in policies[:2]:
        try:
            policy = await http.request(url, limit=3_800_000)
            if domain(policy.url) != domain(page.url):
                continue
            paragraphs = html.fromstring(policy.body).xpath("//p|//li")
            for para in paragraphs[:120]:
                line = clean(para.text_content())
                for match in PUBLISHER_RELATION.finditer(line[:1400]):
                    name = clean(match[1])
                    if bears_product_name(name, product_name):
                        operators.setdefault(name.casefold(), (name, policy.url))
        except (FetchError, ValueError, etree.ParserError):
            continue
    return next(iter(operators.values())) if len(operators) == 1 else None


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
            if discovered := await publisher_evidence(http, website, name):
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


def marketplace_product(row: dict, resolver: Resolver):
    publisher = row.get("publisher", {})
    vendor = clean(publisher.get("displayName"))
    publisher_id = publisher.get("publisherName", "")
    extension = row.get("extensionName", "")
    website = public_url(publisher.get("domain"))
    name = clean(row.get("displayName"))
    if not (publisher.get("isDomainVerified") is True and website and vendor and name):
        return None
    if not all(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", value)
               for value in (publisher_id, extension)):
        return None
    if not MARKETPLACE_TOPIC.search(f"{name} {clean(row.get('shortDescription'))}"):
        return None
    if re.search(r"\b(?:deprecated|nightly|pre-release|research preview|extension pack)\b", name, re.I):
        return None
    versions = row.get("versions") or []
    if not versions:
        return None
    properties = {p["key"]: p.get("value") for p in versions[0].get("properties", [])}
    if properties.get("Microsoft.VisualStudio.Services.Content.Pricing") != "Free":
        return None
    if properties.get("Microsoft.VisualStudio.Code.PreRelease") == "true":
        return None
    stats = {s["statisticName"]: s.get("value") for s in row.get("statistics", [])}
    if (stats.get("install") or 0) < 100:
        return None
    key = f"{publisher_id}.{extension}"
    detail = f"{MARKETPLACE}/items?{urlencode({'itemName': key})}"
    resolved = resolver.resolve(name=vendor, website=website, evidence=detail)
    canonical = resolved.canonical if resolved else vendor
    rule = "Verified Marketplace publisher"
    category = next((c for c in MARKETPLACE_CATEGORIES if c in row.get("categories", [])), "AI")
    content = {
        "productName": name,
        "startupName": canonical,
        "pricingModel": "FREE",
        "sourcePricingModel": "Free",
        "pricingScope": "Marketplace extension access",
        "website": website,
        "category": category,
        "publisherEvidence": {"url": detail, "rule": rule},
    }
    item = record("PRODUCT", "vs-marketplace/" + key.casefold(), "Visual Studio Marketplace", detail, content)
    return item, [(name, name, "Marketplace listing name", detail, detail), (vendor, canonical, rule, detail, detail)]


async def marketplace_products(http: Http, resolver: Resolver):
    seen_ids = set()
    for category in MARKETPLACE_CATEGORIES:
        page = 1
        seen_pages = set()
        while True:
            payload = {
                "filters": [{
                    "criteria": [
                        {"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                        {"filterType": 5, "value": category},
                        {"filterType": 12, "value": "4096"},
                    ],
                    "pageNumber": page, "pageSize": 50, "sortBy": 4, "sortOrder": 0,
                }],
                "assetTypes": [],
                "flags": 788,  # Latest version, properties, category and tags, and statistics.
            }
            data = (await http.request(
                f"{MARKETPLACE}/_apis/public/gallery/extensionquery",
                headers={"Accept": "application/json;api-version=7.2-preview.1"}, payload=payload,
            )).json()["results"][0]["extensions"]
            if not data:
                break
            page_ids = tuple(row["extensionId"] for row in data)
            if page_ids in seen_pages:
                LOG.warning("Marketplace returned the same %s page twice", category)
                break
            seen_pages.add(page_ids)
            for row in data:
                if row["extensionId"] in seen_ids:
                    continue
                seen_ids.add(row["extensionId"])
                if candidate := marketplace_product(row, resolver):
                    yield candidate
            if len(data) < 50:
                break
            page += 1


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
