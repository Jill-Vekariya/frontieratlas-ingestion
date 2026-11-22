import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from frontieratlas.catalogs import marketplace_product, product_candidates, publisher_evidence, startup_rows
from frontieratlas.extraction import Provider, attach_extraction, dense_chunks, enrich, verify_fields
from frontieratlas.http import FetchError, Http, retry_after
from frontieratlas.identity import Resolver
from frontieratlas.model import identifier, publication, public_url, record
from frontieratlas.signals import FEEDS, feed_entries, inspect_page, scan_feed
from frontieratlas.store import Store

END = datetime(2026, 11, 21, 6, 9, 3, tzinfo=UTC)

STARTUP = {
    "id": 100, "url": "https://www.ycombinator.com/companies/startup",
    "name": "Example Corp", "former_names": ["Example AI"], "website": "https://example.com/",
    "status": "Active", "launched_at": 1795150000, "tags": ["AI"], "team_size": 7,
}


class RecordTests(unittest.TestCase):
    def test_source_url_drops_tracking_rejects_internal(self):
        self.assertEqual(public_url("https://example.com/job/1?utm_source=feed&job=3#top"), "https://example.com/job/1?job=3")
        self.assertEqual(public_url("https://user:secret@example.com/"), "")
        self.assertEqual(public_url("http://192.168.1.1:80/"), "")
        self.assertEqual(public_url("http://internal.local/"), "")

    def test_freshness_uses_whole_possible_interval(self):
        self.assertTrue(publication("2026-11-20T11:39:03+05:30").within(END))
        self.assertFalse(publication("2026-11-20T06:09:02+00:00").within(END))
        self.assertFalse(publication("2026-11-21T06:09:04Z").within(END))
        relative = publication("2 hours ago", END)
        self.assertEqual(relative.earliest, END - timedelta(hours=3))
        self.assertTrue(relative.within(END))
        self.assertFalse(publication("24 hours ago", END).within(END))
        self.assertIsNone(publication("2026-11-20T13:00:00"))
        self.assertIsNone(publication("yesterday", END))
        self.assertFalse(publication("2026-11-20", date_in_utc=True).within(END))

    def test_resolver_keeps_conflicts_separate(self):
        rival = dict(STARTUP, id=101, name="Example Europe", former_names=["Example Labs"], website="https://example.eu", url="https://www.ycombinator.com/companies/rival")
        resolver = Resolver([STARTUP, rival])
        self.assertEqual(resolver.resolve("Open AI").canonical, "OpenAI")
        self.assertEqual(resolver.resolve("OpenAI, Inc.").canonical, "OpenAI")
        self.assertEqual(resolver.resolve("Example AI", "https://example.com").canonical, "Example Corp")
        self.assertEqual(resolver.resolve("Example AI", "https://example.eu", "https://example.eu").rule, "Conflicting evidence; source name retained")
        self.assertEqual(resolver.resolve("Example AI").canonical, "Example Corp")
        collision = Resolver([STARTUP, dict(rival, former_names=["Example AI"])])
        self.assertEqual(collision.resolve("Example AI").canonical, "Example AI")

    def test_startup_optional_counts_and_product_deduplication(self):
        first, aliases = next(startup_rows([STARTUP], 1, END))
        self.assertEqual(first["content"]["data"]["employeeCount"], 7)
        self.assertIn(("Example AI", "Example Corp", "Former name in directory", STARTUP["url"], STARTUP["url"]), aliases)
        product = {"name": "Tool", "slug": "tool", "pricingModel": "paid", "website": "https://example.com/tool", "url": "https://example.com/catalog"}
        self.assertEqual(len(list(product_candidates([product, dict(product, name=" TOOL "), dict(product, name="Unknown", pricingModel="maybe")]))), 1)

    def test_article_uses_publication_instead_of_update(self):
        text = b'<article><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-21T03:00:00Z","dateModified":"2026-11-21T05:00:00Z"}</script><p>The publisher gave the original publication timestamp for this article.</p></article>'
        published, method, article = inspect_page(text, "NEWS")
        self.assertEqual((published, method), ("2026-11-21T03:00:00Z", "JSON-LD datePublished"))
        self.assertIn("original publication timestamp", article)
        updated_only = b'<script type="application/ld+json">{"@type":"NewsArticle","dateModified":"2026-11-21T05:00:00Z"}</script>'
        self.assertIsNone(inspect_page(updated_only, "NEWS")[0])

    def test_atom_with_updated_only_is_undated(self):
        feed = next(f for f in FEEDS if f.name == "The Verge")
        xml = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Title</title><link rel="alternate" href="https://example.com/news" /><updated>2026-11-21T04:00:00Z</updated></entry></feed>'
        self.assertIsNone(next(feed_entries(feed, xml)).raw_date)

    def test_marketplace_product_requires_verified_publisher_and_explicit_free_price(self):
        publisher = {"displayName": "Example", "publisherName": "example", "domain": "https://example.com",
                     "isDomainVerified": False}
        price = {"key": "Microsoft.VisualStudio.Services.Content.Pricing", "value": "Trial"}
        raw = {
            "publisher": publisher, "extensionName": "ml", "displayName": "Example ML",
            "shortDescription": "Tools for machine learning",
            "versions": [{"properties": [price]}], "statistics": [{"statisticName": "install", "value": 200}],
        }
        resolver = Resolver([])
        self.assertIsNone(marketplace_product(raw, resolver))
        publisher["isDomainVerified"] = True
        self.assertIsNone(marketplace_product(raw, resolver))
        price["value"] = "Free"
        item, aliases = marketplace_product(raw, resolver)
        self.assertEqual(item["content"]["startupName"], "Example")
        self.assertEqual(item["content"]["pricingModel"], "FREE")
        self.assertEqual(item["source"]["url"], "https://marketplace.visualstudio.com/items?itemName=example.ml")
        self.assertEqual(len(aliases), 2)

    def test_claim_and_export_are_idempotent(self):
        with tempfile.TemporaryDirectory() as out:
            db = Store(Path(out) / "test.sqlite3")
            row = record("JOB", "example.com/job", "Test", "https://example.com/job", {"title": "Engineer"})
            key = identifier("JOB", "example.com/job")
            self.assertTrue(db.claim_signal(key, "first", END))
            self.assertFalse(db.claim_signal(key, "another", END))
            self.assertTrue(db.claim_signal(key, "another", END + timedelta(minutes=11)))
            proof = publication("2026-11-21T05:00:00Z")
            self.assertFalse(db.put(row, publication=proof, owner="first"))
            self.assertTrue(db.put(row, publication=proof, owner="another", body="example"))
            self.assertFalse(db.claim_signal(key, "first", END + timedelta(minutes=20)))
            result = db.export(Path(out), END, [])
            self.assertEqual(result["counts"]["JOB"], 1)
            self.assertEqual(len((Path(out) / "records.jsonl").read_text().splitlines()), 1)
            self.assertEqual(db.export(Path(out), END + timedelta(days=1), [])["counts"]["JOB"], 0)
            db.close()

    def test_evidence_is_grounded_and_byte_limited(self):
        text = "The role is based in Paris. Développer des outils. " * 100
        chunks = dense_chunks("Position", text, 95)
        self.assertTrue(all(len(chunk.encode()) <= 95 for chunk in chunks))
        extracted = {"fields": [
            {"key": "location", "value": "Paris", "evidence": "based in Paris"},
            {"key": "location", "value": "Prague", "evidence": "based in Paris"},
            {"key": "role_family", "value": "Fiction", "evidence": "based in Paris"},
        ]}
        self.assertEqual([x["value"] for x in verify_fields(extracted, text)], ["Paris"])

    def test_retry_after_respects_delay_and_http_date(self):
        self.assertEqual(retry_after("2", END), 2)
        self.assertEqual(retry_after("Sat, 21 Nov 2026 06:10:03 GMT", END), 60)
        self.assertEqual(retry_after("invalid", END), 0)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_terms_must_identify_the_product_operator(self):
        site = "https://example.com"
        pages = {
            site: b'<html><footer>Copyright 2026 Processor LLC. <a href="/terms">Terms of Service</a></footer></html>',
            site + "/terms": b"<p>This service is provided by Example AI LLC.</p><p>Payments are provided by Processor LLC.</p>",
        }

        class Transport:
            async def request(self, url, **_kwargs):
                return SimpleNamespace(url=url, body=pages[url])

        self.assertEqual(await publisher_evidence(Transport(), site, "Example AI"), ("Example AI LLC", site + "/terms"))

    async def test_full_page_is_requested_for_long_feed_excerpt(self):
        feed = next(source for source in FEEDS if source.name == "Ars Technica")
        story = "https://example.com/news"
        paragraph = "A long public article opening. " * 40
        rss = f"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
        <title>Public story</title><link>{story}</link><pubDate>21 Nov 2026 05:00:00 GMT</pubDate>
        <content:encoded><![CDATA[<p>{paragraph}</p><a href="{story}">Read full article</a>]]></content:encoded>
        </item></channel></rss>""".encode()
        page = f"<article><p>{paragraph}</p><p>{'The full story continues. ' * 40}</p></article>".encode()

        class Transport:
            parallel = 4

            async def request(self, url, **_kwargs):
                return SimpleNamespace(body=page if url == story else rss, date=END)

        with tempfile.TemporaryDirectory() as output:
            store = Store(":memory:")
            try:
                await scan_feed(Transport(), feed, store, END, Resolver([]), [])
                store.export(Path(output), END, [])
                content = json.loads((Path(output) / "records.jsonl").read_text())["content"]
                self.assertEqual(content["bodySource"], "Publisher page")
                self.assertGreater(content["bodyCharacterCount"], len(paragraph))
            finally:
                store.close()

    async def test_provider_request_and_response_contracts(self):
        result = {"fields": [{"key": "title", "value": "Public title", "evidence": "Public title"}]}

        class Transport:
            async def request(self, url, *, headers, payload, limit):
                self.call = (url, headers, payload)
                if "x-goog-api-key" in headers:
                    return SimpleNamespace(json=lambda: {
                        "candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]
                    })
                return SimpleNamespace(json=lambda: {
                    "choices": [{"message": {"content": json.dumps(result)}}]
                })

        for name in ("Gemini", "Groq", "DeepSeek"):
            with self.subTest(provider=name):
                transport = Transport()
                provider = Provider(name, "https://example.com/v1", "test-model", "fixture")
                self.assertEqual(await provider.extract(transport, "Public title"), result)
                url, headers, payload = transport.call
                if name == "Gemini":
                    self.assertTrue(url.endswith("/test-model:generateContent"))
                    self.assertEqual(headers.get("x-goog-api-key"), "fixture")
                    self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
                else:
                    self.assertEqual(headers.get("Authorization"), "Bearer fixture")
                    self.assertEqual(payload["response_format"]["type"], "json_object")

    async def test_recent_feed_time_cannot_override_older_job_date(self):
        feed = next(f for f in FEEDS if f.name == "Himalayas")
        listing = "https://himalayas.app/companies/example/jobs/ml-engineer"
        rss = f"""<rss xmlns:h="https://himalayas.app/ns/jobs"><channel><item>
        <title>Machine Learning Engineer</title><link>{listing}</link>
        <h:companyName>Example</h:companyName><pubDate>21 Nov 2026 05:00:00 GMT</pubDate>
        </item></channel></rss>""".encode()
        older = b'<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-11-19T05:00:00Z"}</script>'

        class Transport:
            parallel = 4

            async def request(self, url, **_kwargs):
                return SimpleNamespace(body=older if url == listing else rss, date=END)

        store = Store(":memory:")
        try:
            result = await scan_feed(Transport(), feed, store, END, Resolver([]), [])
            self.assertEqual(result["published"], 0)
            self.assertEqual(result["outsideWindowAfterPageCheck"], 1)
        finally:
            store.close()

    async def test_provider_cannot_replace_verified_catalog_fields(self):
        class Stub:
            name = "Test"
            initial_bytes = 3600

            async def extract(self, _http, text):
                self.text = text
                return {"fields": [
                    {"key": "productName", "value": "Example", "evidence": "Example"},
                    {"key": "startupName", "value": "Different", "evidence": "Different"},
                    {"key": "pricingModel", "value": "FREE", "evidence": "FREE"},
                ]}

        stub = Stub()
        row = record("PRODUCT", "example", "Catalog", "https://example.com/tool",
                     {"productName": "Example", "startupName": None, "pricingModel": "PAID"})
        await attach_extraction(None, [stub], row, "Example\nDifferent products\nFREE trial")
        self.assertIn("Record type PRODUCT", stub.text)
        self.assertIsNone(row["content"]["startupName"])
        self.assertEqual(row["content"]["pricingModel"], "PAID")
        self.assertEqual([x["key"] for x in row["content"]["extractionEvidence"]], ["productName"])

    async def test_413_shrinks_input_and_fallback_preserves_valid_data(self):
        class Stub:
            def __init__(self, name, status=None):
                self.name = name
                self.initial_bytes = 3600
                self.status = status
                self.sizes = []

            async def extract(self, _http, text):
                self.sizes.append(len(text.encode()))
                if self.status is not None:
                    raise FetchError(self.status, "https://example.com")
                return {"fields": [{"key": "location", "value": "Paris", "evidence": "Remote from Paris."}]}

        first, second, third = Stub("One", 413), Stub("Two", 429), Stub("Three")
        fields, provider = await enrich(None, [first, second, third], "Engineer", "Remote from Paris. " * 350)
        self.assertEqual(provider, "Three")
        self.assertGreater(first.sizes[0], first.sizes[-1])
        self.assertEqual(len(second.sizes), 1)
        self.assertEqual(fields[0]["value"], "Paris")

    async def test_429_retries_before_other_requests_proceed(self):
        class Body:
            async def read(self, _size):
                return b"limited"

            async def iter_chunked(self, _size):
                yield b'{"ok":true}'

        class Reply:
            def __init__(self, status):
                self.status = status
                self.headers = {"Retry-After": "0"}
                self.content = Body()
                self.url = "https://example.com/feed"

            async def __aenter__(self): return self
            async def __aexit__(self, *_exc): pass

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, *_args, **_kwargs):
                self.calls.append(asyncio.get_running_loop().time())
                return Reply(429 if len(self.calls) == 1 else 200)

        http = Http(requests_per_second=100)
        http.session = Session()
        result = await http.request("https://example.com/feed")
        self.assertEqual(result.json(), {"ok": True})
        self.assertEqual(len(http.session.calls), 2)
        self.assertGreaterEqual(http.session.calls[1] - http.session.calls[0], 1.0)


if __name__ == "__main__":
    unittest.main()
