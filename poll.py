#!/usr/bin/env python3
"""The Circuit newswire: one run.

Reads every source in sources.yaml, keeps what scores as Gulf-business relevant, drops
what has already been posted or is the same story someone else just filed, and posts one
bundled digest to Slack. Single-shot — one invocation per GitHub Actions tick.

    python3 poll.py                      real run (needs SLACK_WEBHOOK_URL)
    python3 poll.py --dry-run            print the digest, post nothing, touch no state
    python3 poll.py --dry-run --window-hours 24 --source agbi
    python3 poll.py --audit              every source: alive, fresh, parseable
    python3 poll.py --selftest           scoring recall + noise rate against fixtures
    python3 poll.py --score "headline"   score one headline and show which axes hit
    python3 poll.py --test-webhook       assert Slack answers `ok`
    python3 poll.py --status             print status.json

Gate order matters — an item rejected at gate 3 is never seen by gate 4:

    1 fetch      every source, threaded
    2 fresh      within that source's own window
    3 relevant   four-axis score >= the source's threshold
    4 unseen     not already posted, by URL hash and by headline hash
    5 unique     not the story another outlet just filed
    6 post       one bundled message, newest first, capped

Claim-before-send is the load-bearing rule: keys are written to state *before* the post
goes out, and rolled back if Slack refuses. Marking after sending means two overlapping
runs both read an empty store and both send the same digest; not rolling back means a
delivery outage eats the coverage instead of queueing it.

There is deliberately no concurrency lock. The workflow serializes runs, and in the
original two separate outages came from the guard rather than from concurrency — a real
lock wedged permanently when a run was killed while holding it, and its self-expiring
replacement then turned away two scheduled runs.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src import dedup, digest, postlog, state, status
from src.fetch import Item, ParseFailure, fetch_all, now_utc
from src.score import Scorer
from src.slack_client import DeliveryError, SlackClient

load_dotenv()


def _refuse_local_live(args) -> None:
    """Live posting is the cloud's job. One writer, one clock.

    Every duplicate the channel received on 4 September traced to a live run from a
    laptop: 25 state commits that day carried a -04:00 committer offset, and each one wrote
    a stale local view of the dedup store over origin's, so stories the cloud had already
    posted looked new and went out again. Claim-before-send cannot protect two writers
    whose only channel is a git push landing seconds later. So a live run outside GitHub
    Actions is refused unless explicitly forced — and --dry-run needs nothing.
    """
    if getattr(args, "dry_run", False):
        return
    if os.environ.get("GITHUB_ACTIONS") == "true" or getattr(args, "allow_local", False):
        return
    sys.exit("Refusing a LIVE run outside GitHub Actions: a second writer reposts what the "
             "cloud already sent. Use --dry-run, or --allow-local if you truly mean it.")

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
DISPATCH_MINUTES = 15


def load_sources(only: list[str] | None) -> tuple[list[dict], dict]:
    config = yaml.safe_load(SOURCES_FILE.read_text()) or {}
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if only:
        wanted = set(only)
        sources = [s for s in sources if s["key"] in wanted]
        missing = wanted - {s["key"] for s in sources}
        if missing:
            sys.exit(f"unknown source key(s): {', '.join(sorted(missing))}")
    return sources, config


def check_windows(sources: list[dict]) -> list[str]:
    """A window shorter than the dispatch interval loses items in two ways.

    It leaves gaps (a story published between runs falls outside both windows), and it
    means anything trimmed by the digest cap is gone before the next run can pick it up.
    The cap only holds items over safely because the window outlives the interval.
    """
    return [
        s["key"] for s in sources
        if s.get("window_minutes", 360) <= DISPATCH_MINUTES
    ]


def run(args) -> int:
    scorer = Scorer()
    if scorer.config.get("judge"):
        scorer.judge_hook("", "")  # raises loudly — see score.py

    sources, config = load_sources(args.source)
    max_items = args.max_items or config.get("max_items", digest.MAX_ITEMS)
    default_window = config.get("default_window_minutes", 360)
    now = now_utc()
    run_status = status.Run()
    slack = SlackClient()
    mode = "DRY-RUN" if args.dry_run else "LIVE"

    if not args.dry_run and not slack.configured:
        sys.exit("SLACK_WEBHOOK_URL is not set. Use --dry-run to inspect without posting.")

    tight = check_windows(sources)
    if tight:
        print(f"  ! window_minutes <= dispatch interval for: {', '.join(tight)}")

    print(f"[{mode}] {len(sources)} sources, threshold {scorer.default_threshold}, "
          f"cap {max_items}")

    # ---- gate 1 + 2: fetch, within each source's own window -----------------
    candidates: list[Item] = []
    for source, items, entries, exc in fetch_all(sources, now, args.window_hours, default_window):
        key = source["key"]
        if exc is not None:
            if isinstance(exc, ParseFailure):
                run_status.source_parse_fail(key, str(exc))
                print(f"  ! {key:22} PARSE FAILURE: {exc}")
            else:
                run_status.source_error(key, exc)
                print(f"  ! {key:22} {type(exc).__name__}: {exc}")
            continue
        run_status.source_ok(key, len(items), entries)
        print(f"    {key:22} {len(items):3} in window / {entries:3} in feed")
        candidates.extend(items)

    run_status.gate("fetched", len(candidates))

    # ---- reset: forget everything and re-baseline ---------------------------
    # For getting an honest reading of the thing. Clears the claim store and marks
    # whatever is in the feeds right now as already-seen, so from the next tick onward
    # the channel contains only stories published after this moment. Deliberately does
    # NOT merge with origin's state — unioning would resurrect what is being cleared.
    if args.reset and not args.dry_run:
        fresh_state = state.blank()
        keys = [k for item in candidates for k in dedup.keys_for(item)]
        state.claim(fresh_state, keys)
        # Silent. A reset is an operator action, not news, and the channel gets only
        # stories.
        state.record(fresh_state, merge_remote=False)
        run_status.write()
        print(f"Reset — baselined {len(candidates)} items from a clean slate.")
        return 0

    # ---- baseline one source: for undated (html) sources being added -----------
    # Their items age from first sighting, so on the first live run every announcement
    # currently on the listing page looks brand new. Claim them silently instead.
    if args.baseline_source and not args.dry_run:
        wanted = set(args.baseline_source)
        live_state = state.latest()
        keys = [k for item in candidates if item.source_key in wanted
                for k in dedup.keys_for(item)]
        state.claim(live_state, keys)
        state.record(live_state)
        print(f"Baselined {len(keys) // 2} item(s) from {', '.join(sorted(wanted))}; nothing posted.")
        return 0

    # ---- first run: baseline silently rather than dumping the backlog -------
    live_state = state.blank() if args.dry_run else state.latest()
    if not args.dry_run and not state.exists():
        keys = [k for item in candidates for k in dedup.keys_for(item)]
        state.claim(live_state, keys)
        # Silent baseline. The old "watcher is live" marker was another message nobody
        # asked for.
        state.record(live_state)
        run_status.write()
        print(f"First run — baselined {len(candidates)} items, posted hello.")
        return 0

    # ---- gate 2b: undated items age on when we first saw them --------------
    # Some feeds carry no timestamp at all. The freshness gate then never applies to
    # them and they stay eligible forever, reposting endlessly.
    windows = {s["key"]: s.get("window_minutes", default_window) for s in sources}
    fresh: list[Item] = []
    for item in candidates:
        if item.published:
            item.effective_date = item.published
            fresh.append(item)
            continue
        first_seen = state.stamp_first_seen(live_state, dedup.url_key(item))
        item.effective_date = first_seen
        age_minutes = (now - first_seen).total_seconds() / 60
        # Age an undated item on its first sighting, generously — the point is only to
        # stop it living forever, not to race it out of the window on the next tick.
        allowance = max(windows.get(item.source_key, 25), DISPATCH_MINUTES * 2)
        if age_minutes <= allowance:
            fresh.append(item)
    run_status.gate("fresh", len(fresh))

    # ---- gate 3: relevance --------------------------------------------------
    scored: list[Item] = []
    for item in fresh:
        verdict = scorer.score(item.title, item.body, item.author)
        item.score, item.axes = verdict.score, verdict.axes
        if scorer.admits(verdict, item.threshold):
            item.category = scorer.categorize(item.title, item.body)
            scored.append(item)
        elif args.dry_run and args.verbose:
            why = f"veto:{verdict.vetoed}" if verdict.vetoed else f"score {verdict.score}"
            print(f"  · drop [{why}] {item.outlet}: {item.title[:70]}")
    run_status.gate("relevant", len(scored))

    # ---- gate 4: not already posted ---------------------------------------
    seen = live_state["seen"]
    if args.backfill:
        # Release what a baseline swallowed. A first run claims everything currently in
        # the feeds so the channel doesn't get a backlog dump — but afterwards "claimed"
        # is indistinguishable from "posted", so a re-baseline (or a first run on a busy
        # morning) silently buries real coverage. Backfill ignores the claim store for one
        # run and skips only what we can prove was posted: the remembered headlines of
        # past digests, matched near-identically.
        posted_words = [t.get("words", []) for t in live_state["titles"]]

        def already_shown(item: Item) -> bool:
            words = dedup.title_words(item.title)
            return any(dedup.overlap(words, w) >= 0.8 for w in posted_words)

        unseen = [i for i in scored if not already_shown(i)]
        print(f"  backfill: claim store ignored; skipped "
              f"{len(scored) - len(unseen)} already-posted item(s)")
    else:
        unseen = [i for i in scored if not dedup.already_posted(i, seen)]

    # Within-run dedup. Gate 4 above compares against saved state; it cannot catch the
    # same story arriving twice in ONE run, which happens whenever two queries cover the
    # same outlet — a site-scoped NYT query and an entity query that NYT also matched
    # return the same article under different Google redirect URLs, so the URL hashes
    # differ while the headline hash is identical. That posted "LIV Golf plans mass
    # layoffs" twice in a single digest. Keys are only claimed after the digest is built,
    # so nothing earlier in the pipeline notices.
    deduped: list[Item] = []
    run_keys: set[str] = set()
    for item in unseen:
        keys = dedup.keys_for(item)
        if any(k in run_keys for k in keys):
            continue
        run_keys.update(keys)
        deduped.append(item)
    if len(deduped) < len(unseen):
        print(f"  dropped {len(unseen) - len(deduped)} duplicate(s) within this run")
    unseen = deduped
    run_status.gate("unseen", len(unseen))

    # ---- gate 5: not the same story another outlet just filed -------------
    unique: list[Item] = []
    recent = list(live_state["titles"])
    log = postlog.load()
    for item in sorted(unseen, key=lambda i: i.effective_date or now):
        match = dedup.cross_outlet_match(item, recent)
        if match:
            if args.dry_run and args.verbose:
                print(f"  · dup of {match.get('outlet')}: {item.title[:70]}")
            if not args.dry_run:
                # The suppressed copy is evidence about the story we did post: another
                # newsroom thought it worth filing. The briefing ranks on that.
                postlog.bump_corroboration(
                    log, dedup.title_words(item.title), dedup.overlap, dedup.SIMILARITY)
            continue
        unique.append(item)
        recent.append({"words": dedup.title_words(item.title), "at": "", "outlet": item.outlet})
    run_status.gate("unique", len(unique))

    # ---- gate 6: post ------------------------------------------------------
    if not unique:
        # Save the log even with nothing to post: this run may still have recorded
        # corroboration for stories posted earlier, and that is the signal the briefing
        # ranks on most heavily. Discarding it here threw away evidence of exactly the
        # stories several newsrooms agreed were worth filing.
        if not args.dry_run:
            postlog.save(log)
        print("Nothing to post.")
        _housekeeping(run_status, slack, live_state, args)
        return 0

    text, included = digest.build(unique, now, max_items)
    run_status.gate("posted", len(included))

    if args.dry_run:
        print("\n" + "-" * 72 + "\n" + text + "\n" + "-" * 72)
        print(f"\n[DRY-RUN] would post {len(included)} item(s); state untouched.")
        return 0

    # Claim before sending — and only what is actually in this message, so items the cap
    # held over stay unclaimed and the next run can post them.
    claimed = [k for item in included for k in dedup.keys_for(item)]
    remembered = [dedup.title_words(item.title) for item in included]
    state.claim(live_state, claimed)
    for item, words in zip(included, remembered):
        state.remember_title(live_state, words, item.outlet)

    try:
        slack.post(text)
    except DeliveryError as e:
        # Roll back BOTH stores. Unclaiming alone left the headlines remembered, so gate 5
        # went on suppressing other outlets' versions of stories nobody ever received.
        state.unclaim(live_state, claimed)
        state.forget_titles(live_state, remembered)
        run_status.delivered = False
        run_status.failed(e)
        state.record(live_state)
        run_status.write()
        print(f"DELIVERY FAILED: {e}", file=sys.stderr)
        return 1

    run_status.delivered = True
    run_status.posted = len(included)
    # Only after Slack confirmed. The log is what the briefing reads, so an entry in it
    # must mean the desk actually saw the story.
    postlog.record(log, included, dedup.title_words)
    postlog.save(log)
    print(f"Posted {len(included)} item(s).")
    _housekeeping(run_status, slack, live_state, args)
    return 0


def _housekeeping(run_status: status.Run, slack: SlackClient, live_state: dict, args) -> None:
    """Persist state and status. Posts NOTHING.

    This function used to send health alerts — a silence alarm and a dead-feed warning.
    Both are gone. They were never asked for, and on 30 August the dead-feed alert fired
    every five minutes into the live channel for an hour. An automation posts the content
    it was built to post and nothing else; its own health belongs in status.json, which
    `poll.py --status` reads and which is committed to the repo on every run.

    If health alerting is ever wanted, it goes to a separate channel, with a persisted
    cooldown, and only after being asked for.
    """
    if not args.dry_run:
        run_status.write()
        state.record(live_state, files=("watcher_state.json", "status.json", "posted_log.json"))
    else:
        run_status.write()
    doc = status.load()
    print(f"Run took {doc.get('duration_seconds')}s.")


# --------------------------------------------------------------------- subcommands

def cmd_status() -> int:
    print(status.summarize(status.load()))
    return 0


def cmd_score(text: str) -> int:
    scorer = Scorer()
    v = scorer.score(text)
    print(f"score {v.score} (threshold {scorer.default_threshold}) "
          f"-> {'ADMIT' if scorer.admits(v) else 'DROP'}")
    print(f"axes    {', '.join(v.axes) or '(none)'}")
    print(f"matched {', '.join(v.matched) or '(none)'}")
    if v.vetoed:
        print(f"vetoed  {v.vetoed}")
    return 0


def cmd_test_webhook() -> int:
    slack = SlackClient()
    try:
        slack.post(":wrench: Circuit newswire webhook test — delivery confirmed.")
    except DeliveryError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print("Slack accepted the message (`ok`).")
    return 0


def _merge_regression() -> str | None:
    """state.merge() must union two states that both remember headlines.

    This bug shipped twice. The list-in-a-dict-key TypeError was swallowed by both
    callers, so the failure was invisible until stories the cloud had posted came out
    again from a local run. A selftest failure is the alarm that was missing.
    """
    a, b = state.blank(), state.blank()
    state.claim(a, ["u:A"]); state.remember_title(a, ["aramco", "gas"], "Reuters")
    state.claim(b, ["u:B"]); state.remember_title(b, ["adnoc", "ship"], "AGBI")
    try:
        m = state.merge(a, b)
    except Exception as e:  # noqa: BLE001
        return f"state.merge() raised {type(e).__name__}: {e}"
    if set(m["seen"]) != {"u:A", "u:B"} or len(m["titles"]) != 2:
        return f"state.merge() lost data: seen={sorted(m['seen'])} titles={len(m['titles'])}"
    return None


def cmd_selftest() -> int:
    problem = _merge_regression()
    if problem:
        print(f"FAIL   {problem}")
        return 1
    print("ok     state.merge() unions two states with remembered headlines")
    from tests.selftest import main as selftest_main
    return selftest_main()


def cmd_audit(args) -> int:
    from audit_sources import main as audit_main
    return audit_main(args.source)


def main() -> int:
    p = argparse.ArgumentParser(description="The Circuit newswire")
    p.add_argument("--dry-run", action="store_true", help="print, don't post; don't touch state")
    p.add_argument("--verbose", "-v", action="store_true", help="show what was dropped and why")
    p.add_argument("--window-hours", type=float, default=None, help="override every source's window")
    p.add_argument("--source", action="append", help="limit to source key(s); repeatable")
    p.add_argument("--max-items", type=int, default=None, help="override the digest cap")
    p.add_argument("--baseline-source", action="append", metavar="KEY",
                   help="claim a source's current items as seen without posting — run once "
                        "when adding an undated (html) source, or its whole listing posts")
    p.add_argument("--reset", action="store_true",
                   help="clear state and re-baseline: only stories newer than now will post")
    p.add_argument("--allow-local", action="store_true",
                   help="permit a LIVE run outside GitHub Actions (normally refused)")
    p.add_argument("--backfill", action="store_true",
                   help="post what a baseline claimed but never showed (see gate 4)")
    p.add_argument("--status", action="store_true", help="print status.json and exit")
    p.add_argument("--score", metavar="HEADLINE", help="score one headline and exit")
    p.add_argument("--test-webhook", action="store_true", help="post a test message and exit")
    p.add_argument("--selftest", action="store_true", help="scoring recall/noise fixtures")
    p.add_argument("--audit", action="store_true", help="audit every source's feed health")
    args = p.parse_args()

    if args.status:
        return cmd_status()
    if args.score:
        return cmd_score(args.score)
    if args.test_webhook:
        return cmd_test_webhook()
    if args.selftest:
        return cmd_selftest()
    if args.audit:
        return cmd_audit(args)
    _refuse_local_live(args)

    # Any unhandled failure must still leave a status record, or the file keeps showing
    # the last good run and the watcher looks healthy while it is broken — the precise
    # failure this project exists to avoid. Only DeliveryError was covered before.
    try:
        return run(args)
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — recorded, then re-raised untouched
        try:
            failed = status.Run()
            failed.failed(e)
            failed.write()
            if not args.dry_run:
                state.record(state.load())
        except Exception as inner:  # noqa: BLE001
            print(f"  ! could not record failure: {inner}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
