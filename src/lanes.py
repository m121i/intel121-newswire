"""Client lanes: which Intel121 clients a story is for, names only.

Each digest line can carry a short tail — "↳ Dean · Alex" — naming the clients
a story is likely for. The reasons stay off the wire: an analyst replies "why"
(or a client's name) under the digest and the 121 app answers in the thread,
grounded in the same client map. That keeps the wire scannable at a five-minute
tick and puts explanations where they are wanted.

One model call per digest, not per story. The client map (instructions,
follows, watchlist, the team's internal dossier) is read from the Intel121 app
over a read-only endpoint; nothing here writes. Any failure — no key, the map
unreachable, the model slow — degrades to a digest without tails. Lanes must
never delay or block delivery.
"""
from __future__ import annotations

import json
import os
import sys

import requests

MODEL = "claude-opus-4-8"
MAX_PER_STORY = 3
TIMEOUT_S = 40

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "clients": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "clients"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _note(msg: str) -> None:
    print(f"  lanes: {msg}", file=sys.stderr)


def fetch_map() -> list[dict] | None:
    url = os.environ.get("INTEL121_MAP_URL", "").strip()
    key = os.environ.get("INTEL121_MAP_KEY", "").strip()
    if not url or not key:
        return None
    r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
    r.raise_for_status()
    clients = r.json().get("clients") or []
    return clients or None


def short_names(clients: list[dict]) -> dict[str, str]:
    """Full name → what the wire prints: first name when unique, else full name."""
    firsts: dict[str, int] = {}
    for c in clients:
        f = c["name"].split()[0]
        firsts[f] = firsts.get(f, 0) + 1
    return {c["name"]: (c["name"].split()[0] if firsts[c["name"].split()[0]] == 1 else c["name"])
            for c in clients}


def _ask(prompt: str) -> dict:
    import anthropic  # imported here so a missing package only disables lanes
    client = anthropic.Anthropic(timeout=TIMEOUT_S)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except TypeError:
        # an older SDK without output_config: ask for bare JSON instead
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt + "\n\nAnswer with JSON only, "
                       "shaped {\"items\":[{\"index\":0,\"clients\":[\"Full Name\"]}]}."}],
        )
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)


def assign(items: list) -> int:
    """Set item.lanes on each item. Returns how many items got at least one lane.

    Never raises: every failure is a note on stderr and a digest without tails.
    """
    for item in items:
        item.lanes = []
    if not items:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    try:
        clients = fetch_map()
    except Exception as e:  # noqa: BLE001
        _note(f"client map unavailable ({type(e).__name__}: {e}); no lanes this run")
        return 0
    if not clients:
        return 0
    names = short_names(clients)
    roster = "\n".join(f"- {c['name']}: {c['text']}" for c in clients)
    lines = []
    for i, item in enumerate(items):
        summary = (item.body or "").strip().replace("\n", " ")[:220]
        lines.append(f"[{i}] {item.title} ({item.outlet})" + (f" — {summary}" if summary else ""))
    prompt = f"""You route a real-time newswire for Intel121, a private intelligence concierge. For each headline below, name the clients it is genuinely for — the ones whose standing instructions, follows, watchlist or team notes it clearly touches. Names only. Only real matches, never a stretch: a story that merely shares a region or a broad topic with a client is NOT for them. Most headlines match nobody; an empty list is the normal answer. At most {MAX_PER_STORY} clients per headline. Use the client's full name exactly as listed.

CLIENTS:
{roster}

HEADLINES:
{chr(10).join(lines)}"""
    try:
        data = _ask(prompt)
    except Exception as e:  # noqa: BLE001
        _note(f"model call failed ({type(e).__name__}: {e}); no lanes this run")
        return 0
    valid = {c["name"] for c in clients}
    got = 0
    for row in data.get("items", []):
        try:
            idx = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(items):
            continue
        picked = [n for n in row.get("clients", []) if n in valid][:MAX_PER_STORY]
        if picked:
            items[idx].lanes = [names[n] for n in picked]
            got += 1
    return got
