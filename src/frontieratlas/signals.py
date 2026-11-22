"""Feeds, source timestamps, and publisher page extraction."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import uuid4

from lxml import etree, html

from .catalogs import json_nodes
from .extraction import Provider, attach_extraction
from .http import FetchError, Http, Response, batches
from .identity import Resolver
from .model import EMPLOYER_JOB_DATE, Publication, clean, identifier, publication, public_url, record, text_key, url_key, utc
from .store import Store

LOG = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"
HTML_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_AUTHOR = "{http://purl.org/dc/elements/1.1/}creator"
HIMALAYAS = "{https://himalayas.app/ns/jobs}"
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
GREENHOUSE_API = "boards-api.greenhouse.io"
EXCERPT_LINK = re.compile(r"(?:read (?:the )?(?:full )?(?:article|story)|continue reading)[.… ]*", re.I)
AI_ROLE = re.compile(r"\b(?:AI|ML|LLMs?|artificial intelligence|machine learning|deep learning|data scien(?:ce|tist)|computer vision|natural language processing|GenAI|applied scientist|reinforcement learning)\b", re.I)
AI_WORK = re.compile(r"\b(?:train\w*|build\w*|develop\w*|evaluat\w*|deploy\w*|integrat\w*|improv\w*|design\w*)\b[^.!?]{0,45}\b(?:AI (?:models?|systems?|applications?|infrastructure)|machine learning|large language models?|LLMs?|computer vision|foundation models?)\b", re.I)
WIRED_TOPIC = re.compile(r"\b(?:AI|Nvidia|Gemini|Claude|Anthropic|OpenAI|ChatGPT|LLMs?|generative|artificial intelligence)\b", re.I)
ROLES = (
    (r"\b(?:legal|counsel|privacy|compliance)\b", "Legal and Compliance"),
    (r"\b(?:physician|clinician|medical|oncology)\b", "Healthcare"),
    (r"\b(?:product manager|product management|product owner|product director)\b", "Product"),
    (r"\b(?:customer success|partnerships|business development|sales)\b", "Sales and Partnerships"),
    (r"\b(?:data scientist|data science|data analyst|digital analytics|annotation)\b", "Data and Analytics"),
    (r"\b(?:engineer|developer|technical lead|cto)\b", "Engineering"),
    (r"\b(?:research|scientist)\b", "Research"),
    (r"\b(?:designer|creative|audio)\b", "Design and Creative"),
)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: str
    adapter: str


FEEDS = (
    Feed("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", "NEWS", "rss"),
    Feed("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "NEWS", "rss"),
    Feed("Ars Technica", "https://arstechnica.com/ai/feed/", "NEWS", "rss"),
    Feed("WIRED", "https://www.wired.com/feed/tag/ai/latest/rss", "NEWS", "rss"),
    Feed("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "NEWS", "rss"),
    Feed("Remotive", "https://remotive.com/api/remote-jobs", "JOB", "remotive"),
    Feed("Himalayas", "https://himalayas.app/jobs/rss", "JOB", "rss"),
    Feed("Remote OK", "https://remoteok.com/api", "JOB", "remoteok"),
    Feed("Jobicy", "https://jobicy.com/api/v2/remote-jobs?count=100", "JOB", "jobicy"),
    Feed("We Work Remotely", "https://weworkremotely.com/remote-jobs.rss", "JOB", "rss"),
)


@dataclass(frozen=True)
class Listing:
    title: str
    url: str
    raw_date: object
    date_field: str
    summary: str = ""
    body: str = ""
    company: str = ""
    location: str = ""
    categories: tuple[str, ...] = ()
    author: str = ""
    body_is_excerpt: bool = False


def plain(raw: str) -> str:
    if not raw:
        return ""
    try:
        tree = html.fromstring(raw)
        paragraphs = [clean(x.text_content()) for x in tree.xpath(".//p|.//li")]
        return "\n".join(filter(None, paragraphs)) if paragraphs else clean(tree.text_content())
    except (ValueError, etree.ParserError):
        return clean(raw)


def has_read_more(raw: str) -> bool:
    try:
        doc = html.fromstring(raw)
    except (ValueError, etree.ParserError):
        return False
    return any(EXCERPT_LINK.fullmatch(clean(link.text_content())) for link in doc.xpath("//a"))


def feed_entries(feed: Feed, body: bytes):
    if feed.adapter == "rss":
        tree = etree.fromstring(body, etree.XMLParser(resolve_entities=False, no_network=True, recover=False))
        if tree.tag not in {"rss", ATOM + "feed"}:
            raise ValueError("Unrecognized publisher feed")
        for row in tree.findall(".//item") or tree.findall(ATOM + "entry"):
            atom = row.tag == ATOM + "entry"
            link = row.findtext("link") or next((a.get("href") for a in row.findall(ATOM + "link") if a.get("rel") == "alternate"), "")
            title = clean(row.findtext("title") or row.findtext(ATOM + "title"))
            stamp = row.findtext("pubDate") or row.findtext(ATOM + "published")
            tag = "Atom published" if atom else "RSS pubDate"
            summary = plain(row.findtext("description") or row.findtext(ATOM + "summary"))
            raw_article = row.findtext(HTML_CONTENT) or row.findtext(ATOM + "content") or ""
            article = plain(raw_article)
            topics = tuple(clean(x.text) for x in row.findall("category") if x.text)
            author = clean(row.findtext(DC_AUTHOR) or row.findtext(f"{ATOM}author/{ATOM}name"))
            if feed.name == "We Work Remotely":
                company, sep, job = title.partition(": ")
                if sep:
                    yield Listing(job, public_url(link), stamp, tag, body=summary, company=company, location=clean(row.findtext("region")), categories=topics)
            elif feed.name == "Himalayas":
                restrictions = tuple(clean(x.text) for x in row.findall(HIMALAYAS + "locationRestriction") if x.text)
                yield Listing(title, public_url(link), stamp, tag, summary, article, clean(row.findtext(HIMALAYAS + "companyName")), ", ".join(restrictions), topics, author)
            else:
                yield Listing(title, public_url(link), stamp, tag, summary, article, categories=topics, author=author,
                              body_is_excerpt=bool(raw_article and has_read_more(raw_article)))
        return

    data = json.loads(body)
    entries = data[1:] if feed.adapter == "remoteok" else data["jobs"]
    for item in entries:
        if feed.adapter == "jobicy":
            title, company, date_value = item.get("jobTitle"), item.get("companyName"), item.get("pubDate")
            location, topics, content = item.get("jobGeo"), item.get("jobIndustry"), item.get("jobDescription")
            path = "Jobicy pubDate"
        elif feed.adapter == "remotive":
            title, company, date_value = item.get("title"), item.get("company_name"), item.get("publication_date")
            location, topics, content = item.get("candidate_required_location"), item.get("tags"), item.get("description")
            path = "Remotive publication_date"
        else:
            title, company, date_value = item.get("position"), item.get("company"), item.get("date")
            location, topics, content = item.get("location"), item.get("tags"), item.get("description")
            path = "Remote OK date"
        yield Listing(clean(title), public_url(item.get("url")), date_value, path, body=plain(content or ""), company=clean(company), location=clean(location), categories=tuple(topics or ()))


def inspect_page(markup: bytes, kind: str) -> tuple[object, str, str]:
    doc = html.fromstring(markup)
    property_name = "datePosted" if kind == "JOB" else "datePublished"
    item_filter = {"JobPosting"} if kind == "JOB" else {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle"}
    structured_dates = set()
    body = ""
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text or "")
        except ValueError:
            continue
        for node in json_nodes(data):
            types = node.get("@type") or []
            if isinstance(types, str):
                types = [types]
            if not item_filter.intersection(types):
                continue
            if isinstance(value := node.get(property_name), str):
                structured_dates.add(value)
            body = plain(node.get("articleBody") or node.get("description") or "") or body
    if len(structured_dates) == 1:
        publisher_date = next(iter(structured_dates))
        source = f"JSON-LD {property_name}"
    else:
        values = doc.xpath(f'//meta[@itemprop="{property_name}"]/@content')
        if kind == "NEWS":
            values += doc.xpath('//meta[@property="article:published_time"]/@content')
        publisher_date = values[0] if values and len(set(values)) == 1 else None
        source = f"Publisher meta {property_name}"
    for main in doc.xpath('//article|//*[@itemprop="articleBody"]|//main')[:2]:
        paragraphs = [clean(p.text_content()) for p in main.xpath('.//p[not(ancestor::aside) and not(ancestor::footer)]')]
        candidate = "\n".join(dict.fromkeys(x for x in paragraphs if len(x) >= 40))
        if len(candidate) > len(body):
            body = candidate
    return publisher_date, source, body


def greenhouse_job_api(url: str) -> str | None:
    safe = public_url(url)
    if not safe:
        return None
    parts = urlsplit(safe)
    if parts.scheme != "https":
        return None
    if parts.hostname in GREENHOUSE_HOSTS and parts.path == "/embed/job_app":
        query = parse_qs(parts.query)
        boards, ids = query.get("for", []), query.get("token", [])
        if len(boards) != 1 or len(ids) != 1:
            return None
        board, job_id = boards[0], ids[0]
    else:
        if parts.hostname in GREENHOUSE_HOSTS:
            path = parts.path
        elif parts.hostname == GREENHOUSE_API and parts.path.startswith("/v1/boards/"):
            path = parts.path.removeprefix("/v1/boards")
        else:
            return None
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)/jobs/([0-9]+)", path)
        if not match:
            return None
        board, job_id = match.groups()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", board) or not re.fullmatch(r"[0-9]+", job_id):
        return None
    return f"https://{GREENHOUSE_API}/v1/boards/{board.lower()}/jobs/{job_id}"


async def employer_job_date(http: Http, listing: Listing, page: Response) -> tuple[Publication, str] | None:
    direct = greenhouse_job_api(page.url) or greenhouse_job_api(listing.url)
    if direct:
        endpoints = {direct}
    else:
        links = html.fromstring(page.body).xpath("//a/@href")
        endpoints = {url for link in links if (url := greenhouse_job_api(urljoin(page.url, link)))}
    if len(endpoints) != 1:
        return None
    endpoint = endpoints.pop()
    evidence = page if page.url == endpoint else await http.request(endpoint, limit=1_500_000)
    if evidence.url != endpoint:
        return None
    item = evidence.json()
    if not isinstance(item, dict) or str(item.get("id")) != endpoint.rsplit("/", 1)[-1]:
        return None
    if text_key(item.get("title")) != text_key(listing.title):
        return None
    value = item.get("first_published")
    proof = publication(value) if isinstance(value, str) else None
    if proof and proof.precision == "timestamp":
        return proof, endpoint
    return None


def topic(feed: Feed, listing: Listing) -> str | None:
    if feed.kind == "NEWS":
        if feed.name == "WIRED" and not WIRED_TOPIC.search(" ".join((listing.title, listing.summary, *listing.categories))):
            return None
        return "Publisher topic feed"
    checks = ((listing.title, "Title"), (" ".join(listing.categories), "Board category"))
    for value, evidence in checks:
        if match := AI_ROLE.search(value.replace("-", " ")):
            return f"{evidence}: {match.group()}"
    if match := AI_WORK.search(listing.body[:4800]):
        return f"Description: {match.group()}"
    return None


async def listing_pages(http: Http, feed: Feed, end: datetime):
    cursor = None
    seen: set[str] = set()
    for _ in range(25):
        url = feed.url if not cursor else feed.url + "&" + urlencode({"cursor": cursor})
        page = await http.request(url, limit=3_000_000)
        entries = list(feed_entries(feed, page.body))
        yield entries, page.date
        if feed.adapter != "jobicy" or not entries:
            return
        dates = [proof.earliest for row in entries if (proof := publication(row.raw_date, page.date))]
        if dates and min(dates) < end - timedelta(hours=24):
            return
        data = page.json()
        cursor = data.get("nextCursor") if data.get("hasMore") else None
        if not cursor:
            return
        if cursor in seen:
            raise ValueError("Board returned a repeated cursor")
        seen.add(cursor)
    raise ValueError("Board pagination budget reached before the cutoff")


async def scan_feed(http: Http, feed: Feed, store: Store, end: datetime, resolver: Resolver, providers: list[Provider]):
    counts = {"source": feed.name, "kind": feed.kind, "feed": feed.url, "read": 0, "published": 0, "deduplicated": 0, "unverifiedDate": 0}
    owner = uuid4().hex

    async def handle(pair):
        listing, anchor = pair
        match_topic = topic(feed, listing)
        if not listing.url or not listing.title or not match_topic or (feed.kind == "JOB" and not listing.company):
            return
        key = url_key(listing.url)
        row_id = identifier(feed.kind, key)
        proof = publication(listing.raw_date, anchor)
        if proof and not proof.within(end):
            return
        source_date = listing.date_field
        date_url = listing.url
        source_body = "Publisher feed" if listing.body else "Feed summary"
        if listing.body_is_excerpt:
            source_body = "Publisher feed excerpt"
        body = listing.body or listing.summary
        if feed.kind == "JOB":
            evidence = None
            try:
                page = await http.request(listing.url, limit=1_500_000)
                evidence = await employer_job_date(http, listing, page)
                if evidence and page.url != evidence[1]:
                    _, _, fetched_text = inspect_page(page.body, feed.kind)
                    if len(fetched_text) > len(body):
                        body, source_body = fetched_text, "Publisher page"
            except (FetchError, etree.ParserError, ValueError):
                LOG.debug("Employer publication evidence unavailable at %s", feed.name)
            if not evidence:
                store.observe(row_id, listing.url, "No verifiable first-publication timestamp from the employer")
                counts["unverifiedDate"] += 1
                return
            proof, date_url = evidence
            source_date = EMPLOYER_JOB_DATE
            if not proof.within(end):
                store.observe(row_id, listing.url, f"{source_date} at {date_url} is outside the collection window")
                counts["outsideWindowAfterPageCheck"] = counts.get("outsideWindowAfterPageCheck", 0) + 1
                return
            key = f"greenhouse:{url_key(date_url)}"
            row_id = identifier(feed.kind, key)

        if not store.claim_signal(row_id, owner, datetime.now(UTC)):
            counts["deduplicated"] += 1
            return
        try:
            if feed.kind == "NEWS" and (not proof or len(body) < 850 or listing.body_is_excerpt):
                try:
                    page = await http.request(listing.url, limit=1_500_000)
                    value, method, fetched_text = inspect_page(page.body, feed.kind)
                    if publisher_proof := publication(value, page.date):
                        if not proof or (publisher_proof.earliest, publisher_proof.latest) < (proof.earliest, proof.latest):
                            proof, source_date = publisher_proof, method
                    if len(fetched_text) > len(body):
                        body, source_body = fetched_text, "Publisher page"
                except (FetchError, etree.ParserError, ValueError):
                    LOG.debug("Publisher page unavailable at %s", feed.name)
            if not proof:
                store.observe(row_id, listing.url, "No verifiable publication timestamp")
                counts["unverifiedDate"] += 1
                return
            if not proof.within(end):
                store.observe(row_id, listing.url, f"{source_date} puts the listing outside the collection window")
                counts["outsideWindowAfterPageCheck"] = counts.get("outsideWindowAfterPageCheck", 0) + 1
                return

            aliases = []
            content = {
                "title": listing.title,
                "date": utc(proof.earliest),
                "dateEvidence": {"source": source_date, "url": date_url, "earliest": utc(proof.earliest), "latest": utc(proof.latest), "precision": proof.precision},
                "feedUrl": feed.url,
                "topicEvidence": match_topic,
                "bodySource": source_body,
                "bodyCharacterCount": len(body),
                "categories": list(listing.categories),
            }
            if feed.kind == "JOB":
                resolved = resolver.resolve(name=listing.company, evidence=listing.url)
                company = resolved.canonical if resolved else listing.company
                content.update(company=company, is_remote=True, location=listing.location or None)
                content["role_family"] = next((label for pattern, label in ROLES if re.search(pattern, listing.title, re.I)), "Business Operations")
                if resolved:
                    aliases.append((listing.company, company, resolved.rule, listing.url, resolved.evidence))
            else:
                content["author"] = listing.author or None
            output = record(feed.kind, key, feed.name, listing.url, content)
            if providers and body:
                source_text = "\n".join(filter(None, (listing.company, listing.location, listing.author, body)))
                await attach_extraction(http, providers, output, source_text)
            if store.put(output, aliases, proof, body[:100_000], owner):
                counts["published"] += 1
        finally:
            store.release(row_id, owner)

    try:
        async for entries, anchor in listing_pages(http, feed, end):
            counts["read"] += len(entries)
            async for _ in batches(((entry, anchor) for entry in entries), handle, min(8, http.parallel)):
                pass
    except (FetchError, ValueError, KeyError, etree.XMLSyntaxError) as error:
        counts["error"] = str(error)[:150]
        LOG.warning("%s feed ended with %s", feed.name, counts["error"])
    return counts


async def scan_signals(http: Http, store: Store, end: datetime, resolver: Resolver, providers: list[Provider]):
    return await asyncio.gather(*(scan_feed(http, feed, store, end, resolver, providers) for feed in FEEDS))
