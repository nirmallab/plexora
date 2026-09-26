"""What a session may spend, and what it has.

Two currencies an agent pays in: characters of JSON (language tokens) and
pixels of image (vision tokens, roughly width x height / 750). Every packet is
charged when it is ISSUED -- once, however often it is fetched again -- to its
unit and to the session. A unit that has spent its allowance stops asking and
is closed on the best evidence it has.
"""

from __future__ import annotations

import json

from plexora.agent.limits import MAX_TOOL_CHARS

#: Per unit (one marker of one image), unless the session says otherwise.
UNIT_DEFAULT = {"packets": 3, "images": 4, "pixels": 2_500_000, "chars": 12_000}

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
