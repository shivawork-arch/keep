"""KEEP IDs: KEEP-D-001 (decision), KEEP-P-001 (pattern rule), KEEP-I-001 (incident).

Numbers come from a MongoDB counter, never from the model (fixes LORE's messy IDs).
"""

import re
from enum import Enum

from pymongo import ReturnDocument


class Kind(str, Enum):
    DECISION = "D"
    PATTERN = "P"
    INCIDENT = "I"


ID_RE = re.compile(r"^KEEP-([DPI])-(\d{3,})$")


def format_id(kind: Kind, number: int) -> str:
    if number < 1:
        raise ValueError("ID numbers start at 1")
    return f"KEEP-{kind.value}-{number:03d}"


def parse_id(value: str) -> tuple[Kind, int]:
    match = ID_RE.match(value)
    if not match:
        raise ValueError(f"Not a KEEP ID: {value!r}")
    return Kind(match.group(1)), int(match.group(2))


async def next_id(db, kind: Kind) -> str:
    """Atomically take the next number for this kind. Safe if two requests arrive at once."""
    counter = await db.counters.find_one_and_update(
        {"_id": kind.value},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return format_id(kind, counter["seq"])
