"""Does the scorer agree with The Circuit?

The point of this file is that a change to scoring.yaml reports its cost as well as its
benefit. Adding a word to close one miss is easy; noticing that it also opened the door
to forty tabloid items is not, unless something measures it.

Two fixtures:

  headlines_positive.txt  Real Circuit story headlines (their newsletter roundups
                          excluded), pulled from circuit.news. These SHOULD be admitted:
                          they are, by definition, stories The Circuit thought were
                          theirs. Recall is measured against them.
  headlines_negative.txt  Headlines from the Gulf consumer press and general world wires
                          that Circuit did not and would not run — gold rates, school
                          traffic, cricket, non-Gulf corporate news. These should be
                          dropped. The pass rate here is the noise measure.

Recall will never be 100%, and chasing it would wreck the noise number. A miss like
"Dubai, Hong Kong better partners than rivals, Hadi Badri says" is a quote story with no
business vocabulary in it at all; the only word that would catch it is "Dubai", and
admitting on a city name alone is exactly what this scorer is built not to do.

    python3 poll.py --selftest
"""
from __future__ import annotations

from pathlib import Path

from src.score import Scorer

HERE = Path(__file__).resolve().parent
POSITIVE = HERE / "headlines_positive.txt"
NEGATIVE = HERE / "headlines_negative.txt"

# Gates. Below these, something regressed and the run should fail.
MIN_RECALL = 0.60
MAX_NOISE = 0.12


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


def main() -> int:
    scorer = Scorer()
    positives, negatives = _load(POSITIVE), _load(NEGATIVE)
    if not positives or not negatives:
        print("Fixtures missing. See tests/README.md for how to regenerate them.")
        return 1

    # Positives are scored on the headline alone, which is the harder test: at runtime
    # most sources also supply a teaser, so real recall is a little better than this.
    def admits(text: str) -> tuple[bool, object]:
        v = scorer.score(text)
        return scorer.admits(v), v

    hits = [(t, v) for t, v in ((t, admits(t)) for t in positives)]
    kept = [(t, v[1]) for t, v in hits if v[0]]
    missed = [(t, v[1]) for t, v in hits if not v[0]]
    recall = len(kept) / len(positives)

    nhits = [(t, admits(t)) for t in negatives]
    false_pass = [(t, v[1]) for t, v in nhits if v[0]]
    noise = len(false_pass) / len(negatives)

    print(f"recall   {len(kept):3}/{len(positives):3} = {recall:5.1%}  "
          f"(gate {MIN_RECALL:.0%}) — real Circuit stories admitted")
    print(f"noise    {len(false_pass):3}/{len(negatives):3} = {noise:5.1%}  "
          f"(gate {MAX_NOISE:.0%}) — tabloid/off-beat headlines admitted")

    if missed:
        print(f"\nmissed ({len(missed)}):")
        for t, v in missed[:20]:
            axes = ",".join(v.axes) or "-"
            print(f"  {v.score:2} [{axes:28}] {t[:78]}")
        if len(missed) > 20:
            print(f"  … and {len(missed) - 20} more")

    if false_pass:
        print(f"\nfalse passes ({len(false_pass)}):")
        for t, v in false_pass[:20]:
            axes = ",".join(v.axes) or "-"
            print(f"  {v.score:2} [{axes:28}] {t[:78]}")
        if len(false_pass) > 20:
            print(f"  … and {len(false_pass) - 20} more")

    ok = recall >= MIN_RECALL and noise <= MAX_NOISE
    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
