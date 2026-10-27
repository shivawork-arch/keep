import asyncio

from app.models import DecisionIn
from app.search import SearchRequest, search_decisions
from app.store import save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

INPUT = DecisionIn(
    title="Fixed retry intervals",
    decision="Use fixed retry intervals",
    reason="Thundering herd",
    future_implication="No exponential backoff",
    source={"type": "pr", "url": "https://github.com/o/r/pull/42", "ref": "42"},
    decided_by=["alice"],
)


def run(coro):
    return asyncio.run(coro)


def seeded_db(n=2):
    db = FakeDB()
    for _ in range(n):
        run(save_decision(db, FakeDriver(), FakeEmbedder(), INPUT))
    return db


def test_hits_keep_graph_order_and_load_from_mongo():
    db = seeded_db()
    driver = FakeDriver(records=[{"id": "KEEP-D-002", "score": 0.9}, {"id": "KEEP-D-001", "score": 0.7}])
    emb = FakeEmbedder()
    hits = run(search_decisions(db, driver, emb, SearchRequest(text="progressive delay with multiplier")))

    assert [h.decision.id for h in hits] == ["KEEP-D-002", "KEEP-D-001"]
    assert hits[0].score == 0.9
    assert emb.texts == [("query", "progressive delay with multiplier")]


def test_defaults_to_active_only_and_overfetches():
    driver = FakeDriver()
    run(search_decisions(FakeDB(), driver, FakeEmbedder(), SearchRequest(text="retry", k=5)))
    (_, params), = driver.calls
    assert params["statuses"] == ["active"]
    assert params["candidates"] == 20
    assert params["k"] == 5


def test_include_inactive_drops_status_filter():
    driver = FakeDriver()
    run(search_decisions(FakeDB(), driver, FakeEmbedder(),
                         SearchRequest(text="retry", k=10, include_inactive=True)))
    (_, params), = driver.calls
    assert params["statuses"] is None
    assert params["candidates"] == 40


def test_no_hits_returns_empty():
    assert run(search_decisions(FakeDB(), FakeDriver(), FakeEmbedder(), SearchRequest(text="retry"))) == []


def test_graph_id_missing_in_mongo_is_skipped():
    db = seeded_db(1)
    driver = FakeDriver(records=[{"id": "KEEP-D-001", "score": 0.8}, {"id": "KEEP-D-050", "score": 0.6}])
    hits = run(search_decisions(db, driver, FakeEmbedder(), SearchRequest(text="retry")))
    assert [h.decision.id for h in hits] == ["KEEP-D-001"]
