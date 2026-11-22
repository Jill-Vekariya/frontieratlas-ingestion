# FrontierAtlas ingestion

FrontierAtlas collects startups, products, research papers with GitHub star observations, and recent jobs and news. Every exported record carries its source URL. Separate alias records show how the source names map to canonical entities.

[Architecture PDF](architecture.pdf)

## Setup

Use Python 3.11 or later.

```bash
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` in Windows PowerShell or `source .venv/bin/activate` in macOS or Linux. Install and run a small live collection.

```bash
python -m pip install -e .
frontieratlas --startups 3 --products 3 --papers 3 --out out-smoke --require-targets
python -m unittest discover -s tests -v
```

For the full data targets, run the command below. Publisher verification checks official sites. The Marketplace adapter fills remaining product slots with explicitly free extensions from domain-verified publishers.

```bash
frontieratlas --startups 1100 --products 1000 --papers 1050 --verified-product-publishers --arxiv-timestamps --require-targets --out out
```

Use `--mode signals` to refresh news and jobs, or `--mode bulk` to collect the catalogs. The run start fixes the UTC cutoff. A zoned `--cutoff` allows a specific historical window when the public feeds still contain that period. An existing database can be chosen with `--db`. Change `--parallel` to adjust connection concurrency and `--paper-star-hours` to adjust the maximum age of a repository metric.

With `--require-targets`, a bulk shortfall returns exit code 2 after writing the available data and the coverage manifest. A fresh database or a new output path starts a new snapshot. Reusing the database retains previous catalog records and filters jobs and news to the selected window at export.

## Extraction providers

Set one or more of these environment variables to enable evidence-checked extraction for all five record types. Use `--provider-records-per-type 5` for a limited bulk verification run.

| Priority | Credential variable | Default model | Override |
| --- | --- | --- | --- |
| 1 | `GEMINI_API_KEY` | `gemini-3.8-flash` | `GEMINI_MODEL` |
| 2 | `GROQ_API_KEY` | `openai/gpt-oss-120b` | `GROQ_MODEL` |
| 3 | `DEEPSEEK_API_KEY` | `deepseek-flash` | `DEEPSEEK_MODEL` |

The provider chain checks each field against a per-type allowlist and verifies its supporting quote in the supplied source. The pipeline accepts quoted news excerpts and explicitly labelled missing job locations. It also records evidence that corroborates structured fields. Publication times, URLs, prices, canonical names, and star counts come from the source adapters and entity resolver. With no provider credentials, the source adapters and deterministic classifications still run.

## Collection and evidence

| Data | Public inputs | Selection and verification |
| --- | --- | --- |
| Startups | [Y Combinator directory export](https://github.com/yc-oss/api) | Active companies with the AI tag. Positive team sizes come from the directory. |
| Products | [AGIPicks open data](https://agipicks.com/data/) and [Visual Studio Marketplace](https://marketplace.visualstudio.com/vscode) | Unique normalized product names with source-supported prices. Publisher names come from official organization evidence, a matched company domain, or a Marketplace publisher with a verified domain. |
| Papers | [Papers with Code API](https://paperswithcode.co/docs/api) and [arXiv API](https://info.arxiv.org/help/api/user-manual.html) | One record per arXiv ID and normalized title. Prefer official code, then higher stars. Preserve the metric source and observation time. Use arXiv publication timestamps when retrieved. |
| News | TechCrunch, The Verge, Ars Technica, WIRED, MIT Technology Review | Publisher RSS or Atom and original publication fields on available pages. |
| Jobs | Remotive, Himalayas, Remote OK, Jobicy, We Work Remotely | Board feeds and timestamped postings that match AI, machine learning, or data science terms. |

AGIPicks catalog data is attributed to AGIPicks under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The `ENTERPRISE` bucket represents the catalog’s contact-sales model. Marketplace rows report free extension access. A linked hosted service or model API may charge separately. Employee counts are optional when the company directory provides no value.

Publication times are normalized to UTC. Relative dates use a publisher response timestamp and a conservative interval. To enter the export, the full possible publication interval must fall within the last 24 hours. An undated item is tracked by first and last observation for a later evidence check. The signal crawler checks job pages for earlier publication timestamps and records exclusions when a page predates the window. Board-only dates identify candidates that require employer or archived evidence before being treated as original publication dates. Job metadata records eligibility restrictions when provided by the source. When a news feed includes a read-more link, the crawler treats its text as an excerpt and checks the publisher page. It retains excerpt provenance if article text is unavailable.

## Outputs

- `records.jsonl` holds nested records with schema version `1.0`.
- Six CSV tables cover Startups, Products, Research Papers, Jobs, News, and Entity Mapping Log.
- `manifest.json` records the cutoff, source coverage, errors, and row totals.
- `frontieratlas.sqlite3` holds durable records, deduplication leases, acquisition evidence, and metric history. The database may contain source text for internal extraction. Share the metadata exports for submission.

The six-tab [trial workbook](https://docs.google.com/spreadsheets/d/1OG6OkxV7klEeE_L3PhsFRSDz3JcRRcvjvPf7iYM6Ir8/edit) contains 1,100 startups, 1,023 products, 1,050 research papers, 0 verified fresh jobs, 17 news articles, and 4,181 mapping rows. Its signal window runs from 20 November 2026 06:09:03 UTC through 21 November 2026 06:09:03 UTC. The Jobs tab retains the evidence for 36 excluded candidates with earlier matching listings. Later runs use the listings and repository metrics available at the new cutoff.

All 1,050 paper rows use exact arXiv publication timestamps. All 17 news records have publisher-page body evidence, including three Ars Technica pages checked directly during validation. The public export carries article URLs and capture details. The 1,023 product rows comprise 922 AGIPicks products and 101 verified Marketplace extensions. Every product row has a publisher and its evidence URL.

## Runtime design

The asynchronous transport limits connections and paces request starts by origin. It honors Retry-After and falls back after bounded retry budgets. The local ledger delivers one stored row per source identity, with a lease before page extraction and a transaction for the record and its evidence. In production, shard source work by cursor and date range, coordinate quotas across workers, and use PostgreSQL unique constraints and leases for distributed writes.

Use published APIs, RSS, exports, or permitted browser rendering where a publisher offers them. Denied or challenged pages end their fetch, while configured feeds or licensed integrations continue supplying data. The coverage manifest records each feed result and surfaces inaccessible data separately from zero matching items.
