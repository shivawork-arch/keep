import pytest

from app.ids import Kind, format_id, next_id, parse_id


def test_format_pads_to_three_digits():
    assert format_id(Kind.DECISION, 1) == "KEEP-D-001"
    assert format_id(Kind.PATTERN, 42) == "KEEP-P-042"


def test_format_grows_past_999():
    assert format_id(Kind.INCIDENT, 1000) == "KEEP-I-1000"


def test_format_rejects_zero():
    with pytest.raises(ValueError):
        format_id(Kind.DECISION, 0)


@pytest.mark.parametrize("bad", ["#001", "LORE-001", "KEEP-D-1", "KEEP-X-001", "keep-d-001"])
def test_parse_rejects_bad_ids(bad):
    with pytest.raises(ValueError):
        parse_id(bad)


def test_parse_round_trip():
    assert parse_id("KEEP-D-007") == (Kind.DECISION, 7)


class FakeCounters:
    def __init__(self):
        self.seq = {}

    async def find_one_and_update(self, filter, update, upsert, return_document):
        key = filter["_id"]
        self.seq[key] = self.seq.get(key, 0) + update["$inc"]["seq"]
        return {"_id": key, "seq": self.seq[key]}


class FakeDB:
    def __init__(self):
        self.counters = FakeCounters()


async def _take(db, kind, n):
    return [await next_id(db, kind) for _ in range(n)]


def test_next_id_counts_each_kind_separately():
    import asyncio

    db = FakeDB()
    assert asyncio.run(_take(db, Kind.DECISION, 2)) == ["KEEP-D-001", "KEEP-D-002"]
    assert asyncio.run(_take(db, Kind.PATTERN, 1)) == ["KEEP-P-001"]
