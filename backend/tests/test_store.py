import asyncio

from app.models import DecisionIn, Status
from app.store import ensure_schema, get_decision, save_decision, vector_index_query
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

INPUT = DecisionIn(
    title="Fixed retry intervals",
    decision="Use fixed retry intervals",
    rejected="Exponential backoff",
    reason="Thundering herd at 1000+ concurrent requests",
    future_implication="No exponential backoff in retry logic",
    source={"type": "pr", "url": "https://github.com/o/r/pull/42", "ref": "42"},
    decided_by=["alice", "bob"],
    files=["src/api/retry.py"],
)


def run(coro):
    return asyncio.run(coro)


def test_save_writes_both_stores():
    db, driver, emb = FakeDB(), FakeDriver(), FakeEmbedder()
    d = run(save_decision(db, driver, emb, INPUT))

    assert d.id == "KEEP-D-001"
    assert d.status == Status.ACTIVE
    assert d.needs_sync is False
    assert d.history[0].change == "created"

    stored = db.decisions.docs["KEEP-D-001"]
    assert stored["source"]["url"] == "https://github.com/o/r/pull/42"
    assert stored["needs_sync"] is False

    (query, params), = driver.calls
    assert "MERGE (d:Decision {id: $id})" in query
    assert params["id"] == "KEEP-D-001"
    assert params["files"] == ["src/api/retry.py"]
    assert params["people"] == ["alice", "bob"]
    assert params["embedding"] == [0.5] * 4


def test_ids_increase():
    db, driver, emb = FakeDB(), FakeDriver(), FakeEmbedder()
    a = run(save_decision(db, driver, emb, INPUT))
    b = run(save_decision(db, driver, emb, INPUT))
    assert (a.id, b.id) == ("KEEP-D-001", "KEEP-D-002")


def test_neo4j_down_marks_needs_sync_but_keeps_record():
    db = FakeDB()
    d = run(save_decision(db, FakeDriver(fail=True), FakeEmbedder(), INPUT))
    assert d.needs_sync is True
    assert db.decisions.docs[d.id]["needs_sync"] is True


def test_ollama_down_marks_needs_sync_and_skips_graph():
    db, driver = FakeDB(), FakeDriver()
    d = run(save_decision(db, driver, FakeEmbedder(fail=True), INPUT))
    assert d.needs_sync is True
    assert driver.calls == []


def test_embedded_text_contains_rule_and_rejected():
    emb = FakeEmbedder()
    run(save_decision(FakeDB(), FakeDriver(), emb, INPUT))
    assert "Rejected: Exponential backoff" in emb.texts[0]
    assert "Rule: No exponential backoff" in emb.texts[0]


def test_get_round_trip():
    db = FakeDB()
    saved = run(save_decision(db, FakeDriver(), FakeEmbedder(), INPUT))
    loaded = run(get_decision(db, saved.id))
    assert loaded.id == saved.id
    assert loaded.title == INPUT.title
    assert str(loaded.source.url) == "https://github.com/o/r/pull/42"


def test_get_missing_returns_none():
    assert run(get_decision(FakeDB(), "KEEP-D-999")) is None


def test_schema_creates_constraints_and_index():
    driver = FakeDriver()
    run(ensure_schema(driver, 768))
    queries = [q for q, _ in driver.calls]
    assert len(queries) == 4
    assert queries[-1] == vector_index_query(768)
    assert "`vector.dimensions`: 768" in queries[-1]
    assert "'cosine'}}" in queries[-1]
