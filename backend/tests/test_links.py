import asyncio

import pytest

from app.models import DecisionIn
from app.store import LinkError, link_decisions, save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

INPUT = DecisionIn(
    title="Some decision",
    decision="Do the thing",
    reason="Because",
    future_implication="Keep doing the thing",
    source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"},
    decided_by=["alice"],
)


def run(coro):
    return asyncio.run(coro)


def db_with(n):
    db = FakeDB()
    for _ in range(n):
        run(save_decision(db, FakeDriver(), FakeEmbedder(), INPUT))
    return db


def test_link_writes_both_sides_and_graph():
    db, driver = db_with(2), FakeDriver()
    d = run(link_decisions(db, driver, "KEEP-D-002", "KEEP-D-001"))
    assert d.depends_on == ["KEEP-D-001"]
    assert db.decisions.docs["KEEP-D-001"]["blocks"] == ["KEEP-D-002"]
    assert d.history[-1].change == "depends on KEEP-D-001"
    (query, params), = driver.calls
    assert "DEPENDS_ON" in query
    assert params == {"source": "KEEP-D-002", "target": "KEEP-D-001"}


def test_linking_twice_is_a_no_op():
    db, driver = db_with(2), FakeDriver()
    run(link_decisions(db, driver, "KEEP-D-002", "KEEP-D-001"))
    run(link_decisions(db, driver, "KEEP-D-002", "KEEP-D-001"))
    assert db.decisions.docs["KEEP-D-002"]["depends_on"] == ["KEEP-D-001"]
    assert len(driver.calls) == 1


def test_self_link_rejected():
    with pytest.raises(LinkError):
        run(link_decisions(db_with(1), FakeDriver(), "KEEP-D-001", "KEEP-D-001"))


def test_missing_decision_rejected():
    with pytest.raises(LinkError, match="KEEP-D-009"):
        run(link_decisions(db_with(1), FakeDriver(), "KEEP-D-001", "KEEP-D-009"))


def test_direct_loop_rejected():
    db = db_with(2)
    run(link_decisions(db, FakeDriver(), "KEEP-D-002", "KEEP-D-001"))
    with pytest.raises(LinkError, match="Loop"):
        run(link_decisions(db, FakeDriver(), "KEEP-D-001", "KEEP-D-002"))


def test_indirect_loop_rejected():
    db = db_with(3)
    run(link_decisions(db, FakeDriver(), "KEEP-D-003", "KEEP-D-002"))
    run(link_decisions(db, FakeDriver(), "KEEP-D-002", "KEEP-D-001"))
    with pytest.raises(LinkError, match="Loop"):
        run(link_decisions(db, FakeDriver(), "KEEP-D-001", "KEEP-D-003"))


def test_graph_failure_marks_both_for_sync():
    db = db_with(2)
    d = run(link_decisions(db, FakeDriver(fail=True), "KEEP-D-002", "KEEP-D-001"))
    assert d.needs_sync is True
    assert db.decisions.docs["KEEP-D-001"]["needs_sync"] is True
    assert d.depends_on == ["KEEP-D-001"]
