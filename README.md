# intel121-newswire

Watches the publications the Intel121 desk actually pushes from, keeps the fraction that
touches our clients' worlds, and posts one bundled digest to Slack every 15 minutes. Runs
on GitHub Actions, fired by a Google Apps Script clock; nothing depends on a laptop.

Built from the newswire kit The Circuit extracted from its own wire (10 Sep 2026). The
machinery is the kit's; the sources, the vocabulary, the desks and the tests are
Intel121's. Two workspaces, two repos, two clocks: nothing here touches any other
newswire.

## The beat

The worlds Intel121's clients track: Israel and the Middle East, US foreign policy and the
domestic politics of Israel and the Jewish community, Gulf sovereign capital, defense tech
and cyber, Washington's effect on private capital, and sports business. Six desk labels
on every line: Israel/Mideast · US Politics · Gulf Capital · Defense & Cyber · Private
Capital · Sports Business.

## How it decides what matters

Additive keyword scoring, no AI. Anchors: a named institution or company (3), a named
principal (3), a security term (3: ceasefire, sanctions, missile, hostage…), the Jewish
community and the politics of Israel (3), geography (2). Companions: the beat's business,
policy and tech vocabulary (2) and money scale (1). Threshold 4, the headline must hit
two axes, and at least one anchor. A place name never posts on its own.

`python poll.py --selftest` measures recall against the desk's own published card titles
(`tests/headlines_positive.txt`) and the pass rate on an off-beat negative set. Run it
after every edit to `scoring.yaml`.

## Sources

Drawn from what the team posted in #possible-content-to-push over 14 days: the wires
(WSJ, Bloomberg, NYT, CNBC, AP, FT, Reuters, Semafor, Axios, Politico), the Israeli press
(ToI, JPost, Haaretz, Israel Hayom, Ynet, Calcalist, Globes, JTA), the Gulf and regional
press (The National, MEE, AGBI, Forbes ME, Gulf News, Zawya, Al Jazeera, Al-Monitor, Amwaj,
MEED), Iranian and Syrian state media labelled as such, the specialist press (War on the
Rocks, Intelligence Online, Breaking Defense, Defense News, The Record, CyberScoop, DCD,
CSIS, FDD), sports business (Sportico, FOS, ESPN NFL), primary sources (White House, State,
Treasury, SEC, CENTCOM, Israel PMO), and entity searches for the principals and funds the
clients follow. X, YouTube and podcasts were the team's biggest sources and cannot be
polled this way; they stay a human job.

```bash
python poll.py --audit          # every source: alive, fresh, parseable — run monthly
```

## Operating it

```bash
python poll.py --dry-run                       # what would post; touches nothing
python poll.py --dry-run -v --window-hours 3   # …and why each candidate was dropped
python poll.py --selftest                      # recall + noise against fixtures
python poll.py --score "some headline"         # which axes fire, admit or drop
python poll.py --status                        # last run: gates, duration, errors
```

Status endpoint: `https://raw.githubusercontent.com/m121i/intel121-newswire/main/status.json`.

The channel receives stories and nothing else. Health lives in `status.json`.

## Setup

Repo secret `SLACK_WEBHOOK_URL` (an Intel121 Slack app's incoming webhook, bound to the
channel). A Google Apps Script time trigger (`pinger.gs`) fires the workflow every 15
minutes; GitHub's own cron is a laggy fallback. First run baselines silently.

## Layout

```
poll.py              one run: fetch → score → dedup → digest → post → status
sources.yaml         who to read, per-source thresholds and freshness windows
scoring.yaml         the vocabulary, the noise vetoes, the desk routing
audit_sources.py     is every feed alive, fresh and parseable?
src/                 fetch, score, dedup, digest, state, status, slack_client, postlog
tests/               selftest + fixtures (positive = the desk's own card titles)
pinger.gs            the Apps Script clock
```
