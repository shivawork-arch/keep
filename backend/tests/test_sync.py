import asyncio

from app.models import DecisionIn
from app.store import link_decisions, save_decision
from app.sync import repair, sync_check
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


def test_in_step_when_both_writes_worked():
    db, driver = FakeDB(), FakeDriver()
    run(save_decision(db, driver, FakeEmbedder(), INPUT))
    report = run(sync_check(db, driver))
    assert report.in_step


def test_finds_flagged_and_missing():
    db, driver = FakeDB(), FakeDriver()
    run(save_decision(db, driver, FakeEmbedder(), INPUT))                   # D-001 fine
    run(save_decision(db, driver, FakeEmbedder(fail=True), INPUT))          # D-002 flagged, not in graph
    driver.graph_ids.add("KEEP-D-777")                                      # stray graph node
    report = run(sync_check(db, driver))
    assert report.needs_sync == ["KEEP-D-002"]
    assert report.only_in_mongo == ["KEEP-D-002"]
    assert report.only_in_neo4j == ["KEEP-D-777"]
    assert not report.in_step


def test_repair_rebuilds_node_and_links_then_clears_flag():
    db, driver = FakeDB(), FakeDriver()
    run(save_decision(db, driver, FakeEmbedder(), INPUT))
    run(save_decision(db, driver, FakeEmbedder(fail=True), INPUT))
    run(link_decisions(db, FakeDriver(fail=True), "KEEP-D-002", "KEEP-D-001"))

    result = run(repair(db, driver, FakeEmbedder()))
    assert result.repaired == ["KEEP-D-001", "KEEP-D-002"]  # D-001 was flagged by the failed link
    assert result.failed == {}
    assert db.decisions.docs["KEEP-D-002"]["needs_sync"] is False
    link_calls = [p for q, p in driver.calls if "DEPENDS_ON" in q]
    assert {"source": "KEEP-D-002", "target": "KEEP-D-001"} in link_calls
    assert run(sync_check(db, driver)).in_step


def test_repair_keeps_flag_when_still_failing():
    db, driver = FakeDB(), FakeDriver()
    run(save_decision(db, driver, FakeEmbedder(fail=True), INPUT))
    result = run(repair(db, driver, FakeEmbedder(fail=True)))
    assert result.repaired == []
    assert "KEEP-D-001" in result.failed
    assert db.decisions.docs["KEEP-D-001"]["needs_sync"] is True


def test_repair_never_deletes_stray_graph_nodes():
    db, driver = FakeDB(), FakeDriver()
    driver.graph_ids.add("KEEP-D-777")
    run(repair(db, driver, FakeEmbedder()))
    assert "KEEP-D-777" in driver.graph_ids
