#!/usr/bin/env python3
"""Rebuild the scoring fixtures from live sources.

Positives are The Circuit's own story headlines, straight from their WordPress API, with
the Daily and Weekly Circuit roundups excluded — those are link-dumps, not stories, and
their headlines ("The Daily Circuit: Saudi war insurance help + TotalEnergies invests in
UAE") would teach the scorer nothing.

Negatives are the Gulf consumer press and general world wires, unscoped, which is where
the noise this scorer has to reject actually lives — gold rates, school traffic, cricket,
non-Gulf corporate news.

Two curation passes, and the second one matters. A raw harvest from Khaleej Times and
Gulf News contains real Gulf business stories (a bank's rights issue, a port's throughput
record), and leaving those in the negative set would score the scorer down for being
right. They are removed by the ENTITY/BUSINESS filter below. What remains is deliberately
imperfect — a negative set is a judgement about what The Circuit would not run, and the
honest thing is to keep it inspectable rather than pretend it is ground truth.

    python3 tests/regen_fixtures.py
"""
from __future__ import annotations

import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

NEGATIVE_FEEDS = [
    "https://news.google.com/rss/search?q=site:khaleejtimes.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:gulfnews.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en",
]

# A harvested "negative" naming one of these is really a Gulf business story that the
# consumer press happened to run. Excluded rather than counted against the scorer.
LEGIT = re.compile(
    r"\b(ADNOC|Aramco|PIF|Public Investment Fund|Mubadala|ADIA|ADQ|QIA|MGX|G42|"
    r"DP World|AD Ports|Masdar|ACWA|TAQA|Hormuz|sovereign wealth|Etihad Rail|"
    r"Riyadh Air|NEOM|Qiddiya|Diriyah|Tadawul|rights issue|IPO|sukuk|"
    r"joint venture|acquisition|stake)\b",
    re.IGNORECASE,
)


def _raw(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def positives() -> list[str]:
    out: list[str] = []
    for page in (1, 2, 3, 4):
        url = (f"https://circuit.news/wp-json/wp/v2/posts?per_page=100&page={page}"
               "&_fields=title,categories")
        for post in json.loads(_raw(url).decode()):
            # 4 = The Daily Circuit, 8 = The Weekly Circuit — newsletter roundups.
            if {4, 8} & set(post.get("categories", [])):
                continue
            title = _clean(post["title"]["rendered"])
            if len(title) > 15:
                out.append(title)
    return list(dict.fromkeys(out))


def negatives() -> list[str]:
    out: list[str] = []
    for feed in NEGATIVE_FEEDS:
        root = ET.fromstring(_raw(feed))
        local = lambda e: e.tag.split("}")[-1]  # noqa: E731
        for item in root.iter():
            if local(item) != "item":
                continue
            for child in item:
                if local(child) == "title" and child.text:
                    title = re.sub(r"\s+-\s+[^-]{3,40}$", "", _clean(child.text))
                    if len(title) > 15 and not LEGIT.search(title):
                        out.append(title)
    return list(dict.fromkeys(out))


def main() -> int:
    pos, neg = positives(), negatives()
    (HERE / "headlines_positive.txt").write_text(
        "# Real Circuit story headlines — these SHOULD be admitted.\n"
        "# Regenerate: python3 tests/regen_fixtures.py\n" + "\n".join(pos) + "\n"
    )
    (HERE / "headlines_negative.txt").write_text(
        "# Gulf consumer press + general wires Circuit would not run — should be DROPPED.\n"
        "# Curated: harvested items naming a Gulf fund, champion or real deal are removed,\n"
        "# since those are stories the scorer is supposed to admit.\n"
        "# Regenerate: python3 tests/regen_fixtures.py\n" + "\n".join(neg) + "\n"
    )
    print(f"positives {len(pos)}   negatives {len(neg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
