#!/usr/bin/env python3
"""Is every source actually alive?

Run this monthly, and whenever the digest feels thinner than the news does. It is the
only thing in the project that can catch the failure this whole design is shaped around:
**a dead feed answers HTTP 200 and serves a web page.** Nothing errors, nothing warns,
the source just contributes zero items forever. The JI original ran for months with
eight such sources, including its second-most-important paper, which had never delivered
a single item — and one native feed in this project's own list (Arab News) answers 200
today with content sixteen days old.

For each source it reports: HTTP result, whether the body parses as a feed, how many
entries came back, how old the newest one is, and — for Google News sources — whether
the publishers Google returned are the publisher we asked for.

    python3 audit_sources.py                # everything
    python3 audit_sources.py --source agbi   # one source

Read the columns, not the exit code:
    ok           parses, has entries, newest is recent
    STALE        parses and has entries, but nothing recent — the silent killer
    NOT A FEED   served HTML or unparseable markup
    LEAKED       a site: query returned other publishers
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import yaml

from src.fetch import (
    ParseFailure,
    _aware,
    _entry_date,
    _get,
    _gnews_publisher,
    _gnews_url,
    _publisher_matches,
    build_query,
    now_utc,
    parse_feed,
)

SOURCES_FILE = Path(__file__).resolve().parent / "sources.yaml"

# Newest item older than this and the source is reported STALE. Generous on purpose:
# MEED and the state wires legitimately go quiet over a Gulf weekend.
STALE_HOURS = 96
AUDIT_DAYS = 7      # how wide a window to ask Google News for while auditing
LEAK_SHARE = 0.2    # share of wrong-publisher items that makes a site: query untrustworthy


def audit_one(source: dict) -> dict:
    key = source["key"]
    out = {"key": key, "outlet": source.get("outlet", key), "method": source["method"]}
    try:
        if source["method"] == "rss":
            url = source["url"]
        elif source["method"] == "html":
            # An institutional listing page: healthy means it answers and still contains
            # announcement links matching the configured pattern. Site redesigns break the
            # pattern silently — the page keeps returning 200 with zero matches.
            page = _get(source["url"]).text
            links = list(dict.fromkeys(re.findall(source["link_pattern"], page)))
            out["entries"] = len(links)
            if not links:
                out["verdict"] = "NOT A FEED"
                out["detail"] = "listing page answered but matched no announcement links"
            else:
                out["verdict"] = "ok"
                out["detail"] = f"{len(links)} announcement links (undated by design)"
            return out
        else:
            # Built by the same function the fetcher uses — see build_query's docstring.
            query, _ = build_query(source)
            url = _gnews_url(query, AUDIT_DAYS)
        response = _get(url)
    except Exception as e:  # noqa: BLE001 — the report is the product here
        out["verdict"] = "HTTP FAIL"
        out["detail"] = f"{type(e).__name__}: {e}"[:90]
        return out

    try:
        parsed = parse_feed(response.content)
    except ParseFailure as e:
        out["verdict"] = "NOT A FEED"
        out["detail"] = str(e)[:90]
        return out

    dates = [d for d in (_aware(_entry_date(e)) for e in parsed.entries) if d]
    out["entries"] = len(parsed.entries)
    out["dated"] = len(dates)
    newest = max(dates) if dates else None
    out["age_hours"] = round((now_utc() - newest).total_seconds() / 3600, 1) if newest else None

    if source["method"] in ("gnews", "gnews_entity"):
        publishers = Counter(_gnews_publisher(e) or "?" for e in parsed.entries)
        out["publishers"] = publishers.most_common(3)
        _, accept = build_query(source)
        if accept:
            wrong = sum(n for p, n in publishers.items() if not _publisher_matches(p, accept))
            out["leaked"] = wrong
            share = wrong / max(1, len(parsed.entries))
            # The fetcher drops these, so a stray one or two is normal. Flag only when
            # Google has substantially ignored the site: restriction.
            if share >= LEAK_SHARE:
                out["verdict"] = "LEAKED"
                out["detail"] = (f"{wrong}/{len(parsed.entries)} items from other "
                                 f"publishers ({share:.0%})")
                return out

    if not parsed.entries:
        out["verdict"] = "EMPTY"
    elif newest is None:
        out["verdict"] = "NO DATES"
        out["detail"] = "no parseable timestamps — items will age on first-seen instead"
    elif now_utc() - newest > timedelta(hours=STALE_HOURS):
        out["verdict"] = "STALE"
        out["detail"] = f"newest item is {out['age_hours']}h old"
    else:
        out["verdict"] = "ok"
    return out


def main(only: list[str] | None = None) -> int:
    config = yaml.safe_load(SOURCES_FILE.read_text()) or {}
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if only:
        sources = [s for s in sources if s["key"] in set(only)]

    print(f"Auditing {len(sources)} sources (window {AUDIT_DAYS}d, stale after {STALE_HOURS}h)\n")
    print(f"{'source':22} {'method':13} {'verdict':11} {'items':>6} {'newest':>9}  detail")
    print("-" * 100)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(audit_one, sources))

    problems = 0
    for r in sorted(results, key=lambda r: (r["verdict"] == "ok", r["key"])):
        if r["verdict"] != "ok":
            problems += 1
        age = f"{r['age_hours']}h" if r.get("age_hours") is not None else "-"
        print(f"{r['key']:22} {r['method']:13} {r['verdict']:11} "
              f"{r.get('entries', 0):6} {age:>9}  {r.get('detail', '')[:44]}")

    print(f"\n{len(results) - problems}/{len(results)} healthy.")
    if problems:
        print("Anything not `ok` contributes nothing and will not tell you so at runtime.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Audit newswire source health")
    p.add_argument("--source", action="append", help="limit to source key(s)")
    args = p.parse_args()
    sys.exit(main(args.source))
