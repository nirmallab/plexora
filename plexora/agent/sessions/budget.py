"""What a session may spend, and what it has.

Two currencies an agent pays in: characters of JSON (language tokens) and
pixels of image (vision tokens, roughly width x height / PIXELS_PER_TOKEN).
Every packet is charged when it is ISSUED -- once, however often it is fetched
again -- to the session, and a look (the kinds a session names as budgeted)
to its unit too. A unit that has spent its allowance stops asking and is
closed on the best evidence it has.
"""

from __future__ import annotations

import json

from plexora.agent.limits import MAX_TOOL_CHARS

#: Per unit (one marker of one image), unless the session says otherwise.
#: `packets` counts looks. Getting the gate right comes before spending
#: little, so the default is generous: a first look, one beside a reference,
#: a conditional re-look and three rounds of candidates. Each look is a
#: collage and a context sheet (two images, about one and a half million
#: pixels between them with the sheet's 400 µm fields at 384 px), so six looks
#: need twelve images and a little over nine million pixels (a test pins this
#: against the layouts). `chars` allows for about 8k characters of JSON per
#: packet. A marker that reaches this while the evidence still says to go on
#: is not closed on it: the session's limit policy (`schemas.LIMIT_POLICIES`)
#: asks, extends or flags it for review.
UNIT_DEFAULT = {"packets": 6, "images": 12, "pixels": 9_300_000, "chars": 48_000}

#: The bounds a session may set its allowance within.
UNIT_BOUNDS = {"packets": (1, 20), "images": (0, 40), "pixels": (0, None),
               "chars": (1000, None)}

#: A packet's JSON stays well inside one tool result.
PACKET_CHAR_LIMIT = int(0.8 * MAX_TOOL_CHARS)

PIXELS_PER_TOKEN = 750


def empty():
    return {"packets": 0, "images": 0, "pixels": 0, "chars": 0}


def packet_cost(packet: dict, image_sizes) -> dict:
    return {"packets": 1, "images": len(image_sizes),
            "pixels": int(sum(w * h for w, h in image_sizes)),
            "chars": len(json.dumps(packet, default=str))}


def add(total: dict, cost: dict) -> dict:
    for key, value in cost.items():
        total[key] = int(total.get(key, 0)) + int(value)
    return total


def scaled(allowance: dict, factor: int) -> dict:
    """`allowance` granted `factor` times over (a marker's extensions)."""
    return {key: (None if limit is None else int(limit) * int(factor))
            for key, limit in allowance.items()}


def exhausted(used: dict, allowance: dict | None) -> list:
    """The currencies of `allowance` that `used` has reached."""
    allowance = allowance or UNIT_DEFAULT
    return [key for key, limit in allowance.items()
            if limit is not None and used.get(key, 0) >= limit]


def vision_tokens(pixels) -> int:
    return int(-(-int(pixels) // PIXELS_PER_TOKEN))


def trim(packet: dict, order=("shown_ids", "bivariate_detail", "profile_detail")) -> dict:
    """Drop optional evidence, least useful first, until the packet fits."""
    for key in order:
        if len(json.dumps(packet, default=str)) <= PACKET_CHAR_LIMIT:
            break
        packet.get("evidence", {}).pop(key, None)
    return packet
