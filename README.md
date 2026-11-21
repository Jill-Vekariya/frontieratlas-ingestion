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

For the full data targets, run the command below. Vendor metadata requests use the official websites in the product catalog and can increase runtime.

```bash
frontieratlas --startups 1100 --products 1153 --papers 1050 --enrich-vendors --arxiv-timestamps --require-targets --out out
```

Use `--mode signals` to refresh news and jobs, or `--mode bulk` to collect the catalogs. The run start fixes the UTC cutoff. A zoned `--cutoff` allows a specific historical window when the public feeds still contain that period. An existing database can be chosen with `--db`. Change `--parallel` to adjust connection concurrency and `--paper-star-hours` to adjust the maximum age of a repository metric.

With `--require-targets`, a bulk shortfall returns exit code 2 after writing the available data and the coverage manifest. A fresh database or a new output path starts a new snapshot. Reusing the database retains previous catalog records and filters jobs and news to the selected window at export.

## Extraction providers

Set one or more of these environment variables to enable optional enrichment of job location and role family.

| Priority | Credential variable | Default model | Override |
| --- | --- | --- | --- |
| 1 | `GEMINI_API_KEY` | `gemini-2.5-flash` | `GEMINI_MODEL` |
| 2 | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | `GROQ_MODEL` |
| 3 | `DEEPSEEK_API_KEY` | `deepseek-flash` | `DEEPSEEK_MODEL` |

The provider chain accepts an allowlisted result only when its quoted evidence appears in the submitted text. Location values must occur in the quoted span. Role families use a fixed vocabulary. Publication times, URLs, prices, organization aliases, and star counts come from structured source evidence. With no provider credentials, source fields and deterministic classifications remain available.

## Collection and evidence

| Data | Public inputs | Selection and verification |
| --- | --- | --- |
| Startups | [Y Combinator directory export](https://github.com/yc-oss/api) | Active companies with the AI tag. Positive team sizes come from the directory. |
| Products | [AGIPicks open data](https://agipicks.com/data/) | Unique normalized product names with supported pricing categories. Website domains and organization metadata establish publisher names. |
| Papers | [Papers with Code API](https://paperswithcode.co/docs/api) and [arXiv API](https://info.arxiv.org/help/api/user-manual.html) | One record per arXiv ID and normalized title. Prefer official code, then higher stars. Preserve the metric source and observation time. Use arXiv publication timestamps when retrieved. |
| News | TechCrunch, The Verge, Ars Technica, WIRED, MIT Technology Review | Publisher RSS or Atom and original publication fields on available pages. |
| Jobs | Remotive, Himalayas, Remote OK, Jobicy, We Work Remotely | Board feeds and timestamped postings that match AI, machine learning, or data science terms. |

Product data attribution is AGIPicks under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The adapter deduplicates names and normalizes pricing categories. The `ENTERPRISE` bucket represents a contact-sales model reported by the catalog. The recorded pricing check date belongs to the source. Missing publisher names and employee counts are exported as blank values when the source evidence does not support a value.

Publication times are normalized to UTC. Relative dates use a publisher response timestamp and a conservative interval. To enter the export, the full possible publication interval must fall within the last 24 hours. An undated item is tracked by first and last observation for a later evidence check. Job metadata records eligibility restrictions when provided by the board.

## Outputs

- `records.jsonl` holds nested records with schema version `1.0`.
- Six CSV tables cover Startups, Products, Research Papers, Jobs, News, and Entity Mapping Log.
- `manifest.json` records the cutoff, source coverage, errors, and row totals.
- `frontieratlas.sqlite3` holds durable records, deduplication leases, acquisition evidence, and metric history. The database may contain source text for internal extraction. Share the metadata exports for submission.

The six-tab trial workbook represents collection on 21 November 2026. It contains 1,100 startups, 1,153 products, 1,050 research papers, 37 jobs, 17 news articles, and 3,790 mapping rows. Its signal cutoff is 06:09:03 UTC. Later runs reflect the listings and repository metrics exposed by the sources at the new cutoff.

## Runtime design

The asynchronous transport limits connections and paces request starts by origin. It honors Retry-After and falls back after bounded retry budgets. The local ledger delivers one stored row per source identity, with a lease before page extraction and a transaction for the record and its evidence. In production, shard source work by cursor and date range, coordinate quotas across workers, and use PostgreSQL unique constraints and leases for distributed writes.

Use published APIs, RSS, exports, or permitted browser rendering where a publisher offers them. Denied or challenged pages end their fetch, while configured feeds or licensed integrations continue supplying data. The coverage manifest records each feed result and surfaces inaccessible data separately from zero matching items.
