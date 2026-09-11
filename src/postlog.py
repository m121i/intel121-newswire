"""A record of every story the newswire has posted.

The newswire itself needs almost no memory: a hash to know it has seen a URL, and a
stemmed word-set for three hours to spot four outlets filing the same story. Nothing kept
the story *itself*, so anything wanting to look back over a morning — "what were the five
that mattered?" — had nothing to read.

This is that record: append-only, one entry per posted item, committed alongside the
dedup state so it survives the single-shot cloud runs. Fourteen days, which is far more
than the briefing needs and cheap to keep.

One field here is not in the item as posted: `corroboration`, the number of *other*
outlets that filed the same story and were suppressed by the cross-outlet gate. That
counter is the closest thing to an objective measure of a story's weight — when Bloomberg,
Reuters, AGBI and The National all write up the same ADNOC deal within an hour, the
newswire posts one and silently drops three, and until now that signal was thrown away.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Overridable so a test can exercise the briefing against a fabricated period without
# touching the real record. The real log is what the desk actually received; writing
# test data into it would put stories in a briefing that were never posted.
LOG_FILE = Path(os.environ.get("POSTED_LOG")
                or Path(__file__).resolve().parent.parent / "posted_log.json")
KEEP_DAYS = 14
CAP = 3000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load() -> list[dict]:
    """The log, or an empty list. Never raises — a corrupt log must not stop a run."""
    try:
        data = json.loads(LOG_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save(entries: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    kept = [e for e in entries if (_parse(e.get("at", "")) or cutoff) >= cutoff]
    LOG_FILE.write_text(json.dumps(kept[-CAP:], indent=1) + "\n")


def record(entries: list[dict], items: list, words_for) -> None:
    """Append the items just posted.

    `words_for` is dedup.title_words, passed in rather than imported to keep this module
    free of the dedup/fetch import chain — postlog is read by the briefing, which has no
    business pulling in a feed parser.
    """
    for item in items:
        entries.append({
            "at": _now(),
            "title": item.title,
            "url": item.url,
            "outlet": item.outlet,
            "desk": item.desk,
            "category": item.category,
            "score": item.score,
            "axes": list(item.axes or []),
            "paywall": bool(item.paywall),
            "published": item.published.isoformat(timespec="seconds") if item.published else None,
            "words": words_for(item.title),
            "corroboration": 0,
        })


def bump_corroboration(entries: list[dict], words: list[str], overlap_fn, threshold: float) -> bool:
    """Note that another outlet filed a story already in the log.

    Called when the cross-outlet gate suppresses an item: the story it duplicates is the
    one already posted, so the count belongs on that entry. Matches on the same stemmed
    overlap the gate itself used, so a story counted as a duplicate there is the story
    credited here — no second, subtly different notion of sameness.
    """
    for entry in reversed(entries):
        if overlap_fn(words, entry.get("words") or []) >= threshold:
            entry["corroboration"] = int(entry.get("corroboration", 0)) + 1
            return True
    return False


def since(entries: list[dict], hours: float) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [e for e in entries if (_parse(e.get("at", "")) or cutoff) >= cutoff]


def last_brief_at(entries: list[dict]) -> str | None:
    """When the previous briefing ran, stored as a sentinel entry."""
    for entry in reversed(entries):
        if entry.get("kind") == "brief":
            return entry.get("at")
    return None


def mark_brief(entries: list[dict], chosen: int, edition: str | None = None) -> None:
    """Record that a briefing went out, tagged with which edition it was.

    The edition key ("2026-09-04-Morning") is what makes --if-due idempotent: the check
    is "has THIS edition already posted", not "how long since the last one". Without it,
    a run at 6:05 and another at 6:20 both look due.
    """
    entries.append({"at": _now(), "kind": "brief", "chosen": chosen, "edition": edition})


def edition_posted(entries: list[dict], edition: str) -> bool:
    return any(e.get("kind") == "brief" and e.get("edition") == edition for e in entries)


def merge(a: list[dict], b: list[dict]) -> list[dict]:
    """Union two logs, keeping the higher corroboration for a story in both.

    Needed because the newswire and the briefing are separate workflows in separate
    concurrency groups, so they can commit this file at the same moment. Without a merge
    the loser of that race silently overwrote the winner: a simulated race lost the
    briefing's own marker, which would make the next briefing recompute its window from
    an older point and re-post the same five stories. The reverse case drops a story from
    the log, so the next briefing never considers it.

    Keyed on (at, url, kind): a briefing marker has no url, and two stories posted in the
    same second from the same URL are the same entry.
    """
    combined: dict[tuple, dict] = {}
    for entry in list(a) + list(b):
        key = (entry.get("at"), entry.get("url"), entry.get("kind"))
        existing = combined.get(key)
        if existing is None:
            combined[key] = dict(entry)
            continue
        # Corroboration is a counter each writer may have advanced independently; the
        # higher figure is the one that saw more evidence.
        existing["corroboration"] = max(int(existing.get("corroboration", 0)),
                                        int(entry.get("corroboration", 0)))
    return sorted(combined.values(), key=lambda e: e.get("at") or "")
