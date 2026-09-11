"""Fetching and parsing candidate items.

Three discovery methods, chosen per source in sources.yaml:

  rss          A native feed. Preferred: real URLs, real timestamps, real teasers.
  gnews        Site-scoped Google News search, for outlets with no usable feed —
               Bloomberg, Reuters, Zawya, WAM, Khaleej Times, Gulf News, MEED, Arab News.
               Roughly half The Circuit's own sourcing backbone falls in here.
  gnews_entity Entity-scoped Google News search, not restricted to one site: PIF,
               Mubadala, ADNOC, DP World and the rest of the roster, caught wherever
               they surface. This has no counterpart in the JI original.

Three things this module does that a naive port would not:

1. **Parses tolerantly, and reports parse failure as its own state.** Fitch, Argaam,
   ADX, Treasury, OPEC and the World Bank all serve real feeds that a strict XML parser
   rejects outright. Under `xml.etree` those come back as zero items — indistinguishable
   from a dead outlet. feedparser recovers them, and when it cannot, the caller hears
   "parse failure", not "nothing published".

2. **Verifies the publisher on every Google News item.** Google honours `site:` loosely
   and will return other domains. Unchecked, a foreign story arrives wearing the wrong
   outlet's label.

3. **Strips the appended publisher name before anything reads the headline.** Google
   News titles end in " - Publisher". The original hard-coded `" - Times of Israel"`
   while the feed actually sent `" - The Times of Israel"`, so every headline carried
   the outlet name twice for months. Here the suffix comes from the item's own source
   element, never from a hand-typed string.
"""
from __future__ import annotations

import html
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 25
PER_SOURCE_CAP = 80        # items considered per source per run
DEFAULT_WINDOW_MINUTES = 25
MAX_WORKERS = 10


class ParseFailure(RuntimeError):
    """The source answered, but what it served could not be read as a feed."""


@dataclass
class Item:
    source_key: str
    outlet: str
    title: str
    url: str
    published: datetime | None
    body: str = ""
    author: str = ""
    desk: str | None = None
    category: str = "israel_mideast"
    method: str = ""
    paywall: bool = False
    threshold: int | None = None
    # Set by poll.py for undated items, from the persisted first-seen stamp.
    effective_date: datetime | None = None
    score: int = 0
    axes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- text helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def clean(text: str) -> str:
    """Strip tags and entities, normalise the smart-quote mojibake feeds emit."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\x92", "’").replace("\x93", "“").replace("\x94", "”")
    return re.sub(r"\s+", " ", text).strip()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entry_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def canonical(url: str) -> str:
    """Dedup-stable form: no fragment, no query, no trailing slash."""
    u = (url or "").strip().split("#")[0].split("?")[0]
    return u.rstrip("/").lower()


# ------------------------------------------------------------------ http + parse

def _get(url: str, attempts: int = 3) -> requests.Response:
    """GET with a browser UA — Gulf sites and gov feeds 403 short ones.

    Retries only what a retry can fix: connection resets, timeouts, 429 and 5xx (Google
    News does these when hit rapidly). A 404 will not heal, so other 4xx raise at once.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        else:
            if r.status_code != 429 and r.status_code < 500:
                r.raise_for_status()
                return r
            last = requests.HTTPError(f"HTTP {r.status_code} from {url}")
        if i < attempts - 1:
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def parse_feed(body: bytes):
    """Parse a feed body, tolerating malformed markup.

    Raises ParseFailure only when nothing usable came back at all — a feed that is
    merely untidy (feedparser sets `bozo` but still yields entries) is fine, and saying
    otherwise would flag half the Gulf financial press as broken.
    """
    parsed = feedparser.parse(body)
    # An empty feed is not a broken feed. feedparser sets `version` whenever it
    # recognised the document as RSS/Atom, so a valid-but-empty result means the source
    # published nothing in the queried window — Semafor going 33 hours between stories
    # made its 1-day Google query return zero entries, which was being reported as a
    # parse failure. That is both a false alarm and a corruption of the dead-source
    # streak counter, whose whole job is to distinguish these two cases.
    if parsed.entries or getattr(parsed, "version", ""):
        return parsed

    detail = str(getattr(parsed, "bozo_exception", "") or "no entries and no feed type")
    head = body[:400].decode("utf-8", "replace").lower()
    if "<html" in head:
        detail = f"served an HTML page, not a feed ({detail})"
    raise ParseFailure(detail)


# --------------------------------------------------------------------- gnews bits

def _gnews_url(query: str, days: int) -> str:
    q = quote(f"{query} when:{days}d", safe=":()+|\"")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _gnews_publisher(entry) -> str:
    src = getattr(entry, "source", None)
    if isinstance(src, dict):
        return clean(src.get("title", ""))
    return clean(getattr(src, "title", "") if src else "")


def _strip_publisher(title: str, publisher: str) -> str:
    """Remove the ' - Publisher' Google News appends, using the publisher the item
    itself declared. Falls back to dropping any short trailing ' - X' segment.

    This runs before scoring, not just before display: a masthead containing a beat word
    ('Arabian Business', 'Gulf News') would otherwise score on the masthead alone.
    """
    t = title.strip()
    t = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", t)

    # Repeat, because the suffix can appear twice. Semafor bakes "- Semafor" into its own
    # headlines and Google appends its own, so the raw title arrives as
    # "The Gulf faces uneven rebound from war downturn - Semafor - Semafor". A single pass
    # leaves one masthead on the headline, where it then scores as a beat word.
    for _ in range(3):
        stripped = t
        for sep in (" - ", " | "):   # Google uses a dash; some outlets bake in a pipe
            if publisher and stripped.lower().endswith(f"{sep}{publisher.lower()}"):
                stripped = stripped[: -(len(publisher) + len(sep))].strip()
        if stripped == t:
            break
        t = stripped

    t = re.sub(r"\s+[-|]\s+[^-|]{3,40}$", "", t).strip()
    # What is left of "- Semafor" once the masthead goes is not a headline.
    return "" if len(t) < 15 else t


# The Circuit's own domain, excluded from every entity query at the query itself. A
# newswire that hands the desk their own published stories is worse than useless — it
# looks like a source they missed. Sister JI properties are not excluded: they are not
# on this beat and will not surface.
SELF_DOMAINS = ("circuit.news",)

# Appended to every entity query. Google honours negative terms, so the streaming farms
# drop out before they ever reach the scorer.
QUERY_EXCLUSIONS = (
    '-"live stream" -livestream -streaming -"watch live" -"free stream" '
    '-soccer -"match preview" -"full match" -highlights -"tv channel" '
    # Social and UGC platforms. The publisher blocklist already drops these, but Google
    # returns at most 100 entries per query, and a live football fixture involving a club
    # named NEOM fills that budget with tweets and TikToks — starving the real stories
    # the query exists to find.
    '-site:x.com -site:twitter.com -site:instagram.com -site:tiktok.com '
    '-site:facebook.com -site:reddit.com -site:youtube.com'
)

# Publishers never worth reading, regardless of query. Entity queries are not restricted
# to a site — that is the point of them — so a query for NEOM or Diriyah pulls in football
# fixtures and betting tipsters, both of which name Saudi clubs constantly, and a query
# for Qatar Airways pulls in embassy travel notices and content-farm spam.
# Publishers never worth reading. The first three groups are what an unrestricted entity
# query actually drags in, measured rather than imagined: a live query on the ports and
# airlines roster came back 24 of 26 items junk.
PUBLISHER_BLOCKLIST = (
    # Betting and sport — a query for NEOM or Diriyah hits Saudi football clubs.
    "22bet", "1xbet", "betway", "bet365", "sportskeeda", "onefootball", "goal.com",
    "footballcritic", "soccerway", "flashscore", "sofascore", "betting", "tipster",
    "predictions", "fantasy football", "dream11", "today's golfer", "heavy.com",
    "betpawa", "parimatch", "melbet", "stake.com", "oddspedia", "forebet",
    # User-generated and social platforms, which Google News indexes as publishers.
    "facebook", "instagram", "x.com", "twitter", "reddit", "youtube", "linkedin",
    "medium.com", "tiktok", "pinterest", "quora", "substack.com",
    # SEO farms that impersonate airline support desks. These host on legitimate-looking
    # domains — IEEE community pages, waiver forms, health portals — so the publisher name
    # is the only usable signal.
    "ieee", "smartwaiver", "lucent health", "eventbrite", "wixsite", "weebly",
    "blogspot", "wordpress.com", "groups.google", "issuu", "slideshare", "scribd",
    # Aggregators and notice boards seen in live output.
    "mshale", "news on air", "akashvani", "migflug", "travelobiz", "visaguide",
    "parentzone", "totalsportek", "streameast", "sportsurge", "buffstream",
    # Job boards and sport streamers — a query for Talabat returns vacancies, one for
    # Roshn returns Saudi League fixtures.
    "naukrigulf", "bayt.com", "gulftalent", "indeed", "glassdoor", "fancode",
    "tipranks", "foundit", "monster gulf", "laimoon", "jobsora", "talent.com",
)

# Content-farm and support-desk titles. Every pattern here was taken from real output.
_SPAM_TOKEN = re.compile(r"\([A-Za-z0-9]{6,}\)")          # 'Qatar Airways (jbsnBKnqgh)'
_SPAM_TITLE = re.compile(
    r"(?:\+?\d[\d\-\(\)\s]{8,}\d)"                        # a phone number
    r"|\{\{|\}\}|\(\("                                     # template braces
    r"|complete guide|customer (?:service|support|care)|contact support"
    r"|book (?:flight|ticket)|booking number|extra bag|baggage allowance"
    r"|flight status|change flight|refund policy|check-?in online"
    r"|tickets? link|save on extra|days to go until"
    # Pirate sports streaming. These farms name a Saudi club, so they hit an entity and a
    # city and score like a real story: "NEOM v Riyadh Live STREAMING free Soccer Match".
    r"|live ?stream|livestream|streaming|watch ?live|free ?stream|stream(?:s|ing)? on"
    r"|tv channel|live free|free access|full match|match ?thread|kick ?off time"
    r"|soccer match|how to watch",
    re.IGNORECASE,
)

# Styled Unicode — fullwidth (Ｆｒｅｅ) and mathematical alphanumerics (𝐋𝐈𝐕𝐄) — is used
# almost exclusively by content farms evading keyword filters. No newsroom writes headlines
# in it.
_STYLED_UNICODE = re.compile(r"[\uFF01-\uFF5E\U0001D400-\U0001D7FF]")

# A scoreline — "2-1 victory", "beat X 3-0", "1-1 draw" — is a match report. The word
# "port" in a club name is what let one through the business filter.
_SCORELINE = re.compile(r"\b\d{1,2}\s?[-–]\s?\d{1,2}\s+(?:win|victory|draw|defeat|loss|thriller)\b"
                        r"|\b(?:beat|beats|defeats?|thrash(?:es)?)\s+[A-Z][\w\s]{2,25}\s\d{1,2}\s?[-–]\s?\d{1,2}\b",
                        re.IGNORECASE)

# Symbol soup: "%**(Today===)", "~!!【OffiCial】", "+++【FIFA![LIVES'TREAMs!SKY+TV]".
_SYMBOL_SOUP = re.compile(r"[\[\]{}【】!@#~%*+=|]{3,}")

# A sports fixture: two sides joined by "v" or "vs", with a date or year nearby. Catches
# the streaming farms' cleaner titles, which carry no styling or symbols at all.
_FIXTURE = re.compile(
    r"\b\w[\w'\-]*\s+vs?\.?\s+\w[\w'\-]*\b"
    r"(?=.*(?:\b20\d{2}\b|\b\d{1,2}[./]\d{1,2}\b|\b\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)))",
    re.IGNORECASE,
)


def looks_like_spam(title: str) -> bool:
    """Is this a content farm rather than a story?

    Consumer-facing Gulf brands — the airlines above all — attract a large volume of
    fake support-desk pages that Google News indexes as news. They score well on the
    keyword axes (a real airline name, a real country, real business words), so scoring
    cannot catch them and this has to.
    """
    if (_SPAM_TITLE.search(title) or _STYLED_UNICODE.search(title)
            or _SYMBOL_SOUP.search(title) or _FIXTURE.search(title)
            or _SCORELINE.search(title)):
        return True
    match = _SPAM_TOKEN.search(title)
    if not match:
        return False
    token = match.group(0)[1:-1]
    return any(c.islower() for c in token) and any(c.isupper() or c.isdigit() for c in token)


# Arabic letter forms that Google and the outlets themselves use interchangeably. Argaam
# is labelled "أرقام" (hamza-alef) where our list held "ارقام" (plain alef) — different
# code points, so the match failed and 100% of its items were rejected while every request
# returned HTTP 200. Normalising the class beats adding one more spelling: the same trap
# waits in WAM, SPA and QNA's Arabic mastheads.
_ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",   # alef with hamza / madda
    "ى": "ي", "ئ": "ي",                          # alef maqsura, yeh with hamza
    "ة": "ه",                                     # teh marbuta
    "ؤ": "و",
})
_ARABIC_MARKS = dict.fromkeys(range(0x064B, 0x0653))   # harakat


def _fold(text: str) -> str:
    """Comparison form: lowercased, Arabic letter variants and diacritics normalised."""
    return (text or "").lower().translate(_ARABIC_FOLD).translate(_ARABIC_MARKS).strip()


def _publisher_matches(publisher: str, accept: list[str]) -> bool:
    """Google honours site: loosely. Accept only the outlet we asked for."""
    p = _fold(publisher)
    if any(b in p for b in PUBLISHER_BLOCKLIST):
        return False
    if not accept:
        return True
    return any(_fold(a) in p or p in _fold(a) for a in accept if a)


def is_english(title: str) -> bool:
    """Rough script check on a headline.

    Several Gulf sources are labelled by Google with their Arabic masthead — WAM, SPA and
    Argaam all come back as وكالة وام and friends — while the items themselves are the
    English editions, so the publisher name says nothing about the language. This guards
    the other case: a source flipping to its Arabic edition, the way Globes returns 83%
    Hebrew, would otherwise fill the digest with headlines the desk cannot scan.
    """
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.5


# ------------------------------------------------------------------- per method

def _mk(source: dict, title: str, url: str, published: datetime | None,
        body: str, author: str = "") -> Item:
    return Item(
        source_key=source["key"],
        outlet=source.get("outlet") or source["key"],
        title=title,
        url=url,
        published=published,
        body=body,
        author=author,
        desk=source.get("desk"),
        category=source.get("category", "israel_mideast"),
        method=source["method"],
        paywall=bool(source.get("paywall")),
        threshold=source.get("threshold"),
    )


def _fetch_rss(source: dict, since: datetime) -> tuple[list[Item], int]:
    parsed = parse_feed(_get(source["url"]).content)
    items: list[Item] = []
    for e in parsed.entries[:PER_SOURCE_CAP]:
        published = _aware(_entry_date(e))
        if published and published < since:
            continue
        link = (getattr(e, "link", "") or "").strip()
        title = clean(getattr(e, "title", ""))
        if not link or not title:
            continue
        body = clean(getattr(e, "summary", "") or getattr(e, "description", ""))[:800]
        author = clean(getattr(e, "author", ""))
        items.append(_mk(source, title, link, published, body, author))
    return items, len(parsed.entries)


def build_query(source: dict) -> tuple[str, list[str]]:
    """The exact Google News query for a source, plus its accepted publishers.

    Shared with audit_sources.py deliberately. When the audit built its own version of
    the query it tested something the runtime never sends — and Google is sensitive
    enough to phrasing that the two disagreed completely: the bare listings query
    returned zero entries while the runtime's, with the self-exclusion appended,
    returned a hundred. An audit that tests a different string than production sends is
    worse than no audit, because it produces confident wrong answers in both directions.
    """
    if source["method"] == "gnews":
        query = f"site:{source['url']}"
        if source.get("query"):
            query += f" {source['query']}"
        # Accept the domain as well as the masthead. Google labels a story's publisher
        # inconsistently and changes its mind: Gulf News, Khaleej Times and Energy
        # Intelligence all switched from "Gulf News" to "gulfnews.com" between 27 August
        # and 1 September, at which point the publisher check rejected 100% of their items
        # and three sources contributed nothing while reporting HTTP 200. The domain is
        # already the thing the site: query asked for, so accepting it costs no strictness.
        domain = str(source["url"]).replace("www.", "")
        accept = list(source.get("publisher") or [source.get("outlet", "")])
        return query, accept + [domain]

    # gnews_entity — not site-restricted, that being the point of it. The Circuit's own
    # domain is excluded here rather than per source so a new query cannot forget to.
    exclusions = " ".join(f"-site:{d}" for d in SELF_DOMAINS)
    # Streaming-farm negatives too, so that spam is never fetched. Cheaper than filtering
    # it afterwards, and it stops the farms consuming the 100-item cap real stories need.
    # A Saudi club named NEOM guarantees this collision keeps recurring.
    return f"{source['query']} {exclusions} {QUERY_EXCLUSIONS}", []


def _fetch_gnews(source: dict, since: datetime) -> tuple[list[Item], int]:
    days = max(1, math.ceil((now_utc() - since).total_seconds() / 86400))
    query, accept = build_query(source)

    parsed = parse_feed(_get(_gnews_url(query, days)).content)
    items: list[Item] = []
    for e in parsed.entries[:PER_SOURCE_CAP]:
        published = _aware(_entry_date(e))
        if published and published < since:
            continue
        link = (getattr(e, "link", "") or "").strip()
        raw_title = clean(getattr(e, "title", ""))
        if not link or not raw_title:
            continue
        publisher = _gnews_publisher(e)
        if not _publisher_matches(publisher, accept):
            continue  # Google leaked another domain into a site: query
        # Entity queries are not publisher-restricted, which is how a Russian-language
        # republisher ended up credited for a Saudi theme-park story. An English headline
        # from a non-Latin masthead is a reprint, not the outlet that reported it.
        if not accept and not is_english(publisher):
            continue
        title = _strip_publisher(raw_title, publisher)
        if not title or not is_english(title) or looks_like_spam(title):
            continue
        if any(d in link.lower() for d in SELF_DOMAINS):
            continue  # belt and braces: the query already excludes these
        item = _mk(source, title, link, published, "")
        # For an entity query the reporting outlet is whoever filed it, not the query.
        if source["method"] == "gnews_entity" and publisher:
            item.outlet = publisher
        items.append(item)
    return items, len(parsed.entries)


# ------------------------------------------------------------ institutional pages

# Primary sources publish announcements on their own sites and nowhere else useful. ADGM's
# 54%-AUM release went out around 06:00 UTC; the newswire first saw it at 08:01, via
# Zawya. Had no outlet picked it up, it would never have arrived: ADGM has no feed, and
# Google's index of adgm.com is company-registry noise ("Fitness on Reem Island"). DIFC,
# DFSA, PIF, Mubadala and QIA are the same — one to five indexed items a month, none of
# them the announcements. So this method reads the listing page itself.
#
# Items come back UNDATED on purpose. The pages carry a day-granular date ("08 Sep 2026"),
# and a day-granular date fails a 30-minute freshness window by construction — every
# announcement would be "published at midnight" and never post. Undated items instead age
# from when they were first seen, which is exactly the right clock for a primary source:
# it posts when it appears. The page date is used only as a sanity cap, so an old link
# resurfacing in a listing is not mistaken for news.
#
# Adding one of these sources means baselining it first (`poll.py --baseline-source KEY`),
# or its whole current listing posts at once as "new".

HTML_MAX_PAGES = 12          # newest links fetched per run; a listing is rarely deeper
HTML_MAX_AGE_DAYS = 3        # page-dated older than this is a resurfaced link, not news

_OG_TITLE = re.compile(r'property="og:title"\s+content="([^"]+)"', re.IGNORECASE)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DATE = re.compile(r'(?:article:published_time|datePublished)"?\s*(?:content=|:)\s*"([^"]+)"')
_TEXT_DATE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d\d)\b")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _page_title(page: str, strip: list[str]) -> str:
    m = _OG_TITLE.search(page) or _TITLE_TAG.search(page)
    title = clean(m.group(1)) if m else ""
    # Remove a masthead ONLY together with its separator: "DFSA | HKMA and DFSA…" loses
    # "DFSA | ", and "Some headline - ADGM" loses " - ADGM". A bare masthead word at the
    # edge is left alone, because it is usually the subject of the sentence — the first
    # version stripped the bare word and turned "ADGM Reinforces Abu Dhabi's Standing…"
    # into "Reinforces Abu Dhabi's Standing…", "ADGM's FSRA Signs…" into "'s FSRA
    # Signs…", and dropped the entity from "…Accepted Spot Commodity in ADGM", which then
    # scored zero.
    for name in strip:
        n = re.escape(name.strip(" |-–:"))
        if not n:
            continue
        title = re.sub(rf"^\s*{n}\s*[|:\-–]\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(rf"\s*[|:\-–]\s*{n}\s*$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _page_date(page: str) -> datetime | None:
    m = _META_DATE.search(page)
    if m:
        try:
            return _aware(datetime.fromisoformat(m.group(1).replace("Z", "+00:00")))
        except ValueError:
            pass
    m = _TEXT_DATE.search(page)
    if m:
        day, mon, year = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
        try:
            return datetime(year, mon, day, 12, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _fetch_html(source: dict, since: datetime) -> tuple[list[Item], int]:
    listing = _get(source["url"]).text
    links = list(dict.fromkeys(re.findall(source["link_pattern"], listing)))
    if not links:
        # Same failure shape as a feed serving HTML: the page answered and gave nothing
        # usable. Say so, rather than reporting a source that has merely gone quiet.
        raise ParseFailure("listing page yielded no announcement links")

    base = source.get("base") or re.match(r"https?://[^/]+", source["url"]).group(0)
    strip = source.get("title_strip") or [source.get("outlet", "")]
    cutoff = now_utc() - timedelta(days=HTML_MAX_AGE_DAYS)
    items: list[Item] = []
    for link in links[: source.get("max_pages", HTML_MAX_PAGES)]:
        url = link if link.startswith("http") else base + link
        try:
            page = _get(url).text
        except Exception:  # noqa: BLE001 — one dead page must not sink the listing
            continue
        title = _page_title(page, strip)
        if not title or looks_like_spam(title):
            continue
        dated = _page_date(page)
        if dated and dated < cutoff:
            continue
        items.append(_mk(source, title, url, None, "", ""))
    return items, len(links)


_METHODS = {"rss": _fetch_rss, "gnews": _fetch_gnews, "gnews_entity": _fetch_gnews,
            "html": _fetch_html}


def window_start(source: dict, now: datetime, override_hours: float | None = None,
                 default_minutes: int = DEFAULT_WINDOW_MINUTES) -> datetime:
    """Staleness cutoff for one source: how old a story may be and still count as new.

    Not a polling window. Dedup decides novelty; this only decides how far back to look.
    Sizing it like a polling window is a silent-failure generator — at 45 minutes, a
    source whose freshest item is two hours old contributes nothing and never says so.
    See the note on default_window_minutes in sources.yaml for the measured lags.
    """
    if override_hours:
        return now - timedelta(hours=override_hours)
    minutes = source.get("window_minutes", default_minutes)
    return now - timedelta(minutes=minutes)


def fetch_source(source: dict, since: datetime) -> tuple[list[Item], int]:
    """Fetch one source. Returns (items in window, entries seen).

    Raises ParseFailure or a requests error; poll.py catches per source so one bad feed
    cannot sink the run — the project's oldest guarantee.
    """
    fn = _METHODS.get(source["method"])
    if fn is None:
        raise ValueError(f"unknown method {source['method']!r} for {source.get('key')!r}")
    items, seen = fn(source, since)
    return [i for i in items if i.title and i.url], seen


def fetch_all(sources: list[dict], now: datetime, override_hours: float | None = None,
              default_minutes: int = DEFAULT_WINDOW_MINUTES):
    """Fetch every source concurrently. Yields (source, items, entries, exception).

    Threaded because a sequential pass over sixty feeds took the original four minutes,
    against a fifteen-minute dispatch interval.
    """
    def one(source):
        try:
            items, seen = fetch_source(
                source, window_start(source, now, override_hours, default_minutes))
            return source, items, seen, None
        except Exception as e:  # noqa: BLE001 — reported per source, never fatal
            return source, [], 0, e

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        yield from pool.map(one, sources)
