#!/usr/bin/env python3
"""
RSS aggregator.

For every config file in feeds/*.json this script:
  1. Fetches each source RSS/Atom feed (in parallel, with retries).
  2. Normalises every entry into a common shape.
  3. Merges new entries into a persistent archive in data/<app>.json,
     de-duplicating by GUID/link.
  4. Prunes the archive to the last RETENTION_DAYS (default 365) days.
  5. Writes a combined RSS 2.0 feed to public/<app>.xml.
  6. Writes public/index.html listing all generated feeds.

Designed to be run on a schedule (e.g. GitHub Actions every 3 hours).
The script is idempotent: running it twice in a row adds nothing the
second time, because de-duplication is based on stable item IDs rather
than on a time window. That makes it resilient to skipped/failed runs.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import sys
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import requests

# --------------------------------------------------------------------------
# Configuration (override via environment variables in the workflow)
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
FEEDS_DIR = ROOT / "feeds"
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))
# How many items to publish in each output XML file. The archive in
# data/ always keeps the full RETENTION_DAYS window; this only caps the
# published feed so it stays small enough for feed readers to load.
MAX_OUTPUT_ITEMS = int(os.environ.get("MAX_OUTPUT_ITEMS", "500"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "30"))
FETCH_WORKERS = int(os.environ.get("FETCH_WORKERS", "12"))
FETCH_RETRIES = int(os.environ.get("FETCH_RETRIES", "2"))

# The public base URL where the feeds are served (GitHub Pages).
# Used to fill the <atom:link rel="self"> element. Optional.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "").rstrip("/")
SITE_TITLE = os.environ.get("SITE_TITLE", "My Cycling RSS Feeds")

USER_AGENT = (
    "Mozilla/5.0 (compatible; RSS-Aggregator/1.0; +https://github.com/)"
)

NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(days=RETENTION_DAYS)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def to_utc_iso(struct_time) -> str | None:
    """Convert a feedparser time.struct_time to a UTC ISO-8601 string."""
    if not struct_time:
        return None
    try:
        return dt.datetime(*struct_time[:6], tzinfo=dt.timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except ValueError:
        return None


def stable_id(entry, source_url: str) -> str:
    """Build a stable de-duplication key for an entry."""
    for key in ("id", "guid"):
        val = entry.get(key)
        if val:
            return f"id:{val}"
    link = entry.get("link")
    if link:
        return f"link:{link}"
    basis = (source_url + "|" + entry.get("title", "") +
             "|" + (entry.get("published", "") or entry.get("updated", "")))
    return "hash:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def fetch_one(source: dict) -> tuple[dict, list[dict]]:
    """Fetch and parse a single source feed. Returns (source, items)."""
    url = source["url"]
    last_err = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=FETCH_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            items = []
            for e in parsed.entries:
                published = (
                    to_utc_iso(e.get("published_parsed"))
                    or to_utc_iso(e.get("updated_parsed"))
                )
                item = {
                    "id": stable_id(e, url),
                    "title": (e.get("title") or "").strip() or "(no title)",
                    "link": (e.get("link") or "").strip(),
                    "summary": (e.get("summary") or "").strip(),
                    "published": published,        # may be None
                    "source": source.get("name", url),
                    "source_url": url,
                }
                # Optional metadata used as <category> tags.
                for meta in ("country", "platform", "language"):
                    if source.get(meta):
                        item[meta] = source[meta]
                items.append(item)
            log(f"  ok   {len(items):4d}  {source.get('name','')[:48]}")
            return source, items
        except Exception as exc:  # noqa: BLE001 - we want to keep going
            last_err = exc
    log(f"  FAIL  ---  {source.get('name','')[:48]}  ({last_err})")
    return source, []


def load_archive(app: str) -> dict:
    path = DATA_DIR / f"{app}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"  warning: {path} corrupt, starting fresh")
    return {"app": app, "items": {}}


def save_archive(app: str, archive: dict) -> None:
    path = DATA_DIR / f"{app}.json"
    path.write_text(
        json.dumps(archive, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def item_reference_date(item: dict) -> dt.datetime:
    """Date used for sorting and pruning."""
    return (
        parse_iso(item.get("published"))
        or parse_iso(item.get("first_seen"))
        or NOW
    )


# --------------------------------------------------------------------------
# RSS 2.0 output
# --------------------------------------------------------------------------
def rss_date(d: dt.datetime) -> str:
    return format_datetime(d)


def build_rss(app: str, title: str, items: list[dict]) -> str:
    self_url = f"{SITE_BASE_URL}/{app}.xml" if SITE_BASE_URL else ""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(SITE_TITLE)} - {escape(title)}</title>",
        f"<description>Aggregated {escape(title)} feed</description>",
        "<language>en</language>",
        f"<lastBuildDate>{rss_date(NOW)}</lastBuildDate>",
        f"<generator>rss-aggregator</generator>",
    ]
    if self_url:
        parts.append(f'<link>{escape(self_url)}</link>')
        parts.append(
            f'<atom:link href="{escape(self_url)}" rel="self" '
            'type="application/rss+xml"/>'
        )
    else:
        parts.append("<link>https://example.com</link>")

    for it in items:
        pub = item_reference_date(it)
        parts.append("<item>")
        parts.append(f"<title>{escape(it['title'])}</title>")
        if it.get("link"):
            parts.append(f"<link>{escape(it['link'])}</link>")
        guid_val = it.get("link") or it["id"]
        is_perma = "true" if it.get("link") else "false"
        parts.append(
            f'<guid isPermaLink="{is_perma}">{escape(guid_val)}</guid>'
        )
        parts.append(f"<pubDate>{rss_date(pub)}</pubDate>")
        # Source name as a category, plus any metadata categories.
        parts.append(f"<category>{escape(it['source'])}</category>")
        for meta in ("country", "platform", "language"):
            if it.get(meta):
                parts.append(f"<category>{escape(str(it[meta]))}</category>")
        if it.get("summary"):
            # summaries can contain HTML -> wrap in CDATA-safe escaping
            safe = it["summary"].replace("]]>", "]]&gt;")
            parts.append(f"<description><![CDATA[{safe}]]></description>")
        # human-readable source attribution appended to content
        parts.append(
            f'<source url="{escape(it.get("source_url",""))}">'
            f'{escape(it["source"])}</source>'
        )
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Per-app processing
# --------------------------------------------------------------------------
def process_app(config_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    app = config_path.stem
    title = config.get("title", app.title())
    sources = config.get("sources", [])
    log(f"\n=== {app}  ({len(sources)} sources) ===")

    archive = load_archive(app)
    items_by_id: dict[str, dict] = archive.get("items", {})

    # Fetch all sources in parallel.
    new_count = 0
    with cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for _src, items in pool.map(fetch_one, sources):
            for it in items:
                existing = items_by_id.get(it["id"])
                if existing:
                    # keep original first_seen, refresh mutable fields
                    it["first_seen"] = existing.get("first_seen")
                    items_by_id[it["id"]] = it
                else:
                    it["first_seen"] = NOW.isoformat()
                    items_by_id[it["id"]] = it
                    new_count += 1

    # Prune to retention window.
    before = len(items_by_id)
    items_by_id = {
        k: v for k, v in items_by_id.items()
        if item_reference_date(v) >= CUTOFF
    }
    pruned = before - len(items_by_id)

    archive["app"] = app
    archive["title"] = title
    archive["updated"] = NOW.isoformat()
    archive["items"] = items_by_id
    save_archive(app, archive)

    # Build sorted item list (newest first) for output.
    ordered = sorted(
        items_by_id.values(), key=item_reference_date, reverse=True
    )
    published_items = ordered[:MAX_OUTPUT_ITEMS]

    xml = build_rss(app, title, published_items)
    (PUBLIC_DIR / f"{app}.xml").write_text(xml, encoding="utf-8")

    log(f"  new: {new_count}  pruned: {pruned}  "
        f"archive: {len(items_by_id)}  published: {len(published_items)}")

    return {
        "app": app,
        "title": title,
        "sources": len(sources),
        "archive": len(items_by_id),
        "published": len(published_items),
        "new": new_count,
    }


def build_index(summaries: list[dict]) -> None:
    rows = []
    for s in sorted(summaries, key=lambda x: x["app"]):
        link = f"{s['app']}.xml"
        rows.append(
            f"<tr><td><a href='{link}'>{html.escape(s['title'])}</a></td>"
            f"<td>{s['sources']}</td><td>{s['archive']}</td>"
            f"<td>{s['published']}</td></tr>"
        )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(SITE_TITLE)}</title>
<style>
body{{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#1a1a1a}}
h1{{font-size:1.5rem}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #e2e2e2}}
th{{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:#666}}
a{{color:#0b66c3;text-decoration:none}} a:hover{{text-decoration:underline}}
small{{color:#888}}
</style></head><body>
<h1>{html.escape(SITE_TITLE)}</h1>
<p><small>Last updated: {html.escape(NOW.strftime('%Y-%m-%d %H:%M UTC'))} ·
Retention: {RETENTION_DAYS} days · Max items per feed: {MAX_OUTPUT_ITEMS}</small></p>
<table>
<thead><tr><th>Feed</th><th>Sources</th><th>Archived items</th><th>Published items</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody></table>
<p><small>Subscribe by adding the feed URL (the .xml links above) to your RSS reader.</small></p>
</body></html>"""
    (PUBLIC_DIR / "index.html").write_text(page, encoding="utf-8")
    # Prevent GitHub Pages from running Jekyll on the output.
    (PUBLIC_DIR / ".nojekyll").write_text("", encoding="utf-8")


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    PUBLIC_DIR.mkdir(exist_ok=True)

    configs = sorted(FEEDS_DIR.glob("*.json"))
    if not configs:
        log("No feed configs found in feeds/")
        return 1

    summaries = [process_app(c) for c in configs]
    build_index(summaries)

    log("\n=== summary ===")
    for s in summaries:
        log(f"{s['app']:10s} sources={s['sources']:4d} "
            f"new={s['new']:4d} archive={s['archive']:5d} "
            f"published={s['published']:4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
