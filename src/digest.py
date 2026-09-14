"""Message formatting: one bundled digest per run.

    • *<url|Headline>*: Outlet (Desk) — Category · 4m ago
    _One-sentence summary, only when the source provides a real one._

Bold runs to the end of the headline and stops; the attribution trails it unbolded. The
headline leads because the story is what you scan for — the outlet and category are there
to qualify it, and in an earlier layout they pushed the headline into the middle of the
line. There is no header: Slack already stamps every message with the app name and time.

The summary rule is the one to keep exactly: **show a summary or show nothing.** Feeds
hand you three kinds of text and only one is publishable.

  A real summary   — publish it, trimmed to its first complete sentence.
  A wire lede      — "KAMPALA, Aug 6 (Reuters) - Uganda's parliament on Thursday..."
                     Reject: it reads as a fragment cut mid-thought.
  Boilerplate      — "The post <headline> appeared first on <site>", which every
                     WordPress feed emits. Reject.
"""
from __future__ import annotations

import re
from datetime import datetime

from .fetch import Item, clean

PREVIEW_CHARS = 180
MAX_ITEMS = 20

# No single source may take more than this many slots in one digest. Argaam alone filed
# twelve of twenty in the first live dry-run — Saudi market disclosures down to a
# SAR 392,000 deal — which is how a firehose crowds out the story you actually needed.
# The JI original had a per-feed cap and removed it; with a bundled twenty-item message
# and a source list this uneven in volume, it earns its place back.
MAX_PER_SOURCE = 4

# Order the digest reads in. Categories travel with each line rather than heading a
# block, so the groups still read together without emoji headers.
CATEGORY_ORDER = ["israel_mideast", "us_politics", "gulf_capital", "defense_cyber", "private_capital", "sports_business"]
CATEGORY_LABELS = {
    "israel_mideast": "Israel/Mideast",
    "us_politics": "US Politics",
    "gulf_capital": "Gulf Capital",
    "defense_cyber": "Defense & Cyber",
    "private_capital": "Private Capital",
    "sports_business": "Sports Business",
}


def escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_age(published: datetime | None, now: datetime) -> str:
    """"4m ago". Empty when there is no date — never a fabricated one.

    Worth noting the original printed nothing here for its whole life: it subtracted a
    date from a string, got NaN, and silently rendered an empty age on every digest line.
    """
    if not published:
        return ""
    minutes = round((now - published).total_seconds() / 60)
    if minutes < 0:
        return ""
    if minutes < 60:
        return f"{minutes}m ago"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {rest}m ago" if rest else f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def preview_for(item: Item) -> str:
    """The outlet's own summary, cut to one sentence, or "".

    Returns "" more often than you would expect: feeds with no description at all,
    Google News items whose description is just the headline plus publisher in HTML, and
    anything that fails the three wire-lede tells below.
    """
    desc = clean(item.body or "")
    if len(desc) < 40:
        return ""

    # A blurb that merely restates the headline adds nothing to the line above it.
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower())  # noqa: E731
    title_norm = norm(clean(item.title))
    if title_norm and norm(desc).startswith(title_norm[:40]):
        return ""

    # WordPress auto-generates this for FDD/JINSA-class feeds. Pure noise.
    if re.match(r"^The post .+ appeared first on ", desc, re.IGNORECASE):
        return ""

    head = desc[:90]
    if re.match(r"^By\s+[A-Z]", head):                                    # byline
        return ""
    if re.match(r"^[A-Z][A-Z .'\-]{2,30},?\s+\w{3,9}\s+\d{1,2}", head):   # CITY, Aug 6
        return ""
    if re.search(r"\((Reuters|AP|AFP|Bloomberg|WAM|SPA|QNA|KUNA)\)\s*[-–—]", head, re.I):
        return ""

    # Must be a complete sentence that fits. Anything needing truncation is an excerpt,
    # not a summary, so it is dropped rather than posted as a fragment.
    match = re.match(r"^(.{40,}?[.!?])(\s|$)", desc)
    if not match:
        return ""
    sentence = match.group(1)
    return sentence if len(sentence) <= PREVIEW_CHARS else ""


def format_line(item: Item, now: datetime) -> str:
    notes = []
    if item.desk:
        notes.append(item.desk)
    if item.paywall:
        notes.append("paywall")
    note = f" ({', '.join(notes)})" if notes else ""
    age = format_age(item.effective_date or item.published, now)
    label = CATEGORY_LABELS.get(item.category, item.category)

    line = (
        f"• *<{item.url}|{escape(clean(item.title))}>*: "
        f"{escape(item.outlet)}{escape(note)} — {label}"
        f"{' · ' + age if age else ''}"
    )
    preview = preview_for(item)
    if preview:
        line += f"\n_{escape(preview)}_"
    # client lanes: names only; the reasons live in the thread ("why")
    lanes = getattr(item, "lanes", None) or []
    if lanes:
        line += "\n↳ " + " · ".join(escape(n) for n in lanes)
    return line


def build(items: list[Item], now: datetime, max_items: int = MAX_ITEMS,
          max_per_source: int = MAX_PER_SOURCE) -> tuple[str, list[Item]]:
    """Render the digest, newest first within each category.

    Returns the text and the items actually included — the caller claims exactly those,
    because anything trimmed by either cap must stay unclaimed. In the original, items
    past the cap were marked sent and silently discarded forever rather than held for the
    next run, which is a quiet way to lose coverage. Here they stay unclaimed, and the
    per-source freshness window outlives the dispatch interval, so the next run picks
    them up rather than losing them.
    """
    def sort_key(i: Item):
        return i.effective_date or i.published or now

    # Strictly newest first, across every category.
    #
    # This used to group by category and sort within each group, which is how the JI
    # original read. On this beat it actively misleads: a five-hour-old Gulf business item
    # would sit above a fifteen-minute-old Hormuz story purely because of its section, so
    # the digest looked stale even when it was leading with fresh news. The category label
    # still travels on each line, so nothing is lost by ordering on recency alone.
    ordered = sorted(items, key=sort_key, reverse=True)

    # Per-source cap, applied newest-first so a firehose keeps its freshest items.
    per_source: dict[str, int] = {}
    capped: list[Item] = []
    for item in ordered:
        n = per_source.get(item.source_key, 0)
        if n >= max_per_source:
            continue
        per_source[item.source_key] = n + 1
        capped.append(item)
    ordered = capped

    included = ordered[:max_items]
    text = "\n".join(format_line(i, now) for i in included)
    if len(ordered) > max_items:
        text += f"\n_+{len(ordered) - max_items} more held for the next run._"
    return text, included
