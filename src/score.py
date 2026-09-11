"""Relevance scoring: additive keyword points across four axes.

Terms carry points, points sum, and a per-source threshold admits the story. No AI.

The shape of this is the whole design, and it came out of reading 269 of The Circuit's
own headlines. Geography is NOT the beat. Of eleven Egypt stories they published, ten
carry a business or technology term — a tire factory, an IMF payout, a minerals survey,
an airport terminal, a startup fund, Cairo stocks. Egypt never appears as Egypt; it
appears as a venue for capital. So:

    geography            2 points — cannot clear the threshold alone
    named entity         3 points — a company or fund IS business by definition
    named principal      3 points — MBS, MBZ, Tahnoun, Al-Rumayyan, Al Mubarak
    sector               2 points — business, ventures, technology, science
    conflict economics   3 points — Hormuz, rerouting, war-risk insurance
    money scale          1 point  — $, billion, million, percentages

At the default threshold of 4, "Saudi Arabia arrests cleric" scores 2 and dies, while
"Egypt seeks bids to build a $500 million tire factory" scores 5. A named entity needs
just one companion signal, so "ADNOC hires Squarepoint's Roulon to lead trading arm"
clears at 5.

Two mechanisms carried over from the JI original: a per-source threshold, for throttling
a high-volume outlet without removing it; and a byline bypass, so a named reporter's work
surfaces regardless of score.

And the limitation, stated plainly rather than discovered later: this is literal word
matching. It cannot tell how central a subject is — one passing mention scores the same
as a whole article — it cannot tell reporting from commentary about reporting, and it
cannot catch a story that avoids its vocabulary. Every gap in the original was closed by
adding another word after a miss. `judge` in scoring.yaml is the dormant seam where a
language model scoring relevance by meaning would go instead; see `judge_hook()`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).resolve().parent.parent / "scoring.yaml"

# Term boundaries that tolerate the punctuation in real names: "e&", "AI", "Ma'aden",
# "2PointZero". A plain \b breaks on the ampersand and would match "ai" inside "Dubai".
_LEFT = r"(?<![A-Za-z0-9])"
# A trailing plural is allowed, so the word lists can stay singular. Without this,
# "investments" silently failed to match "investment" — a whole class of quiet misses
# that looked like vocabulary gaps and was really a matcher bug.
_RIGHT = r"(?:s|es)?(?![A-Za-z0-9])"


@dataclass
class Verdict:
    score: int
    axes: list[str]
    matched: list[str]
    vetoed: str | None = None
    bypass: str | None = None
    # Axes hit by the headline alone. The anchor test uses these, not `axes`.
    title_axes: list[str] = field(default_factory=list)

    @property
    def admitted(self) -> bool:
        return self.vetoed is None and (self.bypass is not None or self.score > 0)


def _compile(terms: list[str]) -> re.Pattern:
    parts = [_LEFT + re.escape(str(t).strip()) + _RIGHT for t in terms if str(t).strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!x)x")


class Scorer:
    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else yaml.safe_load(CONFIG_FILE.read_text())
        self.default_threshold = int(self.config.get("default_threshold", 4))
        self.require_anchor = set(self.config.get("require_anchor", []) or [])
        self.min_title_axes = int(self.config.get("min_title_axes", 0))
        self.axes: list[tuple[str, int, re.Pattern]] = []
        for axis in self.config.get("axes", []):
            self.axes.append((axis["name"], int(axis["points"]), _compile(axis.get("terms", []))))
        # Money scale is a pattern, not a word list — "$1.2bn", "20%", "billion".
        self.money = re.compile(self.config.get("money_pattern", r"\$|\bbillion\b|\bmillion\b|\d+%"),
                                re.IGNORECASE)
        self.money_points = int(self.config.get("money_points", 1))
        self.noise = _compile(self.config.get("noise", []))
        self.bylines = [b.lower() for b in self.config.get("byline_bypass", [])]
        # Section routing, from the same vocabulary the scorer already reads.
        # Section routing: `categories` in scoring.yaml, checked in order, first match
        # wins; `default_category` catches the rest. Generic so a beat with different
        # desks (Intel121 has six) only edits the data file.
        cats = self.config.get("categories", {})
        self._cats = [(name, _compile(terms)) for name, terms in cats.items()]
        self._default_cat = str(self.config.get("default_category") or (list(cats)[-1] if cats else "general"))

    # ---- the gate -----------------------------------------------------------

    def score(self, title: str, body: str = "", author: str = "") -> Verdict:
        """Score one item.

        Title and body are scored together, which is how the original worked and is the
        right call for a keyword system: a headline alone is often too terse to carry
        two axes ("ADNOC's XRG targets US, Latin America" needs the teaser to know it is
        an expansion story).
        """
        text = f" {title} {body} ".replace("’", "'")

        noise_hit = self.noise.search(title)  # veto reads the headline only, not the teaser
        if noise_hit:
            return Verdict(0, [], [], vetoed=noise_hit.group(0))

        # Axes are scored on title + body, but tracked separately for the title, because
        # the anchor test below reads only the headline. A feed description is enough to
        # carry an off-beat story past the bar otherwise: FT's "Israel considers expelling
        # UK from postwar Gaza headquarters" and "Syria's Kurds dissolve military force"
        # both score 2 on their headline and cleared the threshold on body text alone.
        title_text = f" {title} ".replace("’", "'")
        total = 0
        axes_hit: list[str] = []
        title_axes: list[str] = []
        matched: list[str] = []
        for name, points, pattern in self.axes:
            hits = pattern.findall(text)
            if hits:
                total += points
                axes_hit.append(name)
                matched += [h if isinstance(h, str) else h[0] for h in hits[:3]]
            if pattern.search(title_text):
                title_axes.append(name)
        if self.money.search(text):
            total += self.money_points
            axes_hit.append("money")

        bypass = self._byline(author)
        # Dedupe matched terms, preserving order, and keep the list short enough to log.
        seen: dict[str, None] = {}
        for m in matched:
            seen.setdefault(m.strip().lower(), None)
        return Verdict(total, axes_hit, list(seen)[:6], bypass=bypass, title_axes=title_axes)

    def threshold_for(self, source_threshold: int | None) -> int:
        return int(source_threshold) if source_threshold is not None else self.default_threshold

    def admits(self, verdict: Verdict, source_threshold: int | None = None) -> bool:
        if verdict.vetoed:
            return False
        if verdict.bypass:
            return True
        # No anchor axis, no story — see require_anchor in scoring.yaml. Checked before
        # the threshold so a high score on sector plus money cannot carry a story from
        # outside the region.
        if self.require_anchor and not (self.require_anchor & set(verdict.title_axes)):
            return False
        # The headline must carry two signals, not just a place name. A country alone is a
        # war-and-diplomacy story: FT's "Syria's Kurds dissolve military force and
        # integrate fighters with Damascus" anchors on Syria and nothing else, and cleared
        # the bar on body text. Costs 0.4 points of recall and buys the whole class.
        if self.min_title_axes and len(set(verdict.title_axes)) < self.min_title_axes:
            return False
        return verdict.score >= self.threshold_for(source_threshold)

    def _byline(self, author: str) -> str | None:
        a = (author or "").lower()
        if not a:
            return None
        return next((b for b in self.bylines if b in a), None)

    # ---- categorisation -----------------------------------------------------

    def categorize(self, title: str, body: str = "") -> str:
        """Which digest section a story belongs in.

        Derived from the story, not from its source: the same outlet files a Pentagon
        contract and a Senate primary, and the reader wants those in different places.
        The order of `categories` in scoring.yaml is a priority list.
        """
        text = f" {title} {body} "
        for name, rx in self._cats:
            if rx.search(text):
                return name
        return self._default_cat

    # ---- the dormant seam ---------------------------------------------------

    def judge_hook(self, title: str, body: str) -> None:
        """Where a meaning-based relevance judge would go.

        Deliberately unimplemented. `judge: true` in scoring.yaml is refused loudly
        rather than silently ignored, so nobody can believe an AI pass is running when
        it is not. The pattern to copy when the word list starts feeling like a
        treadmill is `ji-govt-watcher/src/classifier.py`: Gemini 2.5 Flash on the free
        tier, structured verdict, no Anthropic key, no per-item cost worth measuring.
        """
        raise NotImplementedError(
            "scoring.yaml sets judge: true, but no judge is implemented. "
            "Set it back to false, or port ji-govt-watcher/src/classifier.py."
        )
