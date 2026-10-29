import asyncio

from app.graph import graph
from app.models import DecisionIn
from app.patterns import PatternIn, save_pattern
from app.store import link_decisions, save_decision, supersede
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder


def run(c):
    return asyncio.run(c)


def dec(title, files, who):
    return DecisionIn(title=title, decision=f"Use {title}", reason="Because of load", future_implication=f"Keep {title}",
                      source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"},
                      decided_by=[who], files=files)


def test_graph_nodes_and_edges():
    db, drv, emb = FakeDB(), FakeDriver(), FakeEmbedder()
    run(save_decision(db, drv, emb, dec("fixed retries", ["src/retry.py"], "alice")))   # D-001
    run(save_decision(db, drv, emb, dec("batch backoff", ["src/retry.py"], "bob")))     # D-002
    run(save_decision(db, drv, emb, dec("redis cache", ["src/cache.py"], "alice")))     # D-003
    run(link_decisions(db, drv, "KEEP-D-003", "KEEP-D-002"))
    run(supersede(db, drv, "KEEP-D-001", "KEEP-D-002"))
    run(save_pattern(db, drv, PatternIn(rule="Use X | None", anti_pattern="Optional", language="python", reason="Modern typing")))
    db.incidents.docs["KEEP-I-001"] = {"_id": "KEEP-I-001", "title": "Retry storm", "decisions": ["KEEP-D-002", "KEEP-D-404"]}

    g = run(graph(db))
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    assert kinds["KEEP-D-001"] == "decision" and kinds["file:src/retry.py"] == "file"
    assert kinds["person:alice"] == "person" and kinds["KEEP-P-001"] == "pattern" and kinds["KEEP-I-001"] == "incident"
    edges = {(e["source"], e["target"], e["kind"]) for e in g["edges"]}
    assert ("KEEP-D-001", "file:src/retry.py", "GOVERNS") in edges
    assert ("person:bob", "KEEP-D-002", "DECIDED") in edges
    assert ("KEEP-D-003", "KEEP-D-002", "DEPENDS_ON") in edges
    assert ("KEEP-D-002", "KEEP-D-001", "SUPERSEDES") in edges
    assert ("KEEP-I-001", "KEEP-D-002", "RELATES_TO") in edges
    assert not any(e["target"] == "KEEP-D-404" for e in g["edges"])  # dangling links dropped
    d1 = next(n for n in g["nodes"] if n["id"] == "KEEP-D-001")
    assert d1["status"] == "superseded" and d1["history"]
