import asyncio
import json
from types import SimpleNamespace

import httpx

from app.commit_keeper import CommitRequest, ZERO, breaking_risk, domains_for, keep_commit, summarise
from tests.fakes import FakeDB, FakeDriver

S = SimpleNamespace(ollama_url="http://ollama:11434", summary_model="llama3.2:3b")


def run(c):
    return asyncio.run(c)


def test_domains():
    assert domains_for(["tests/test_a.py", "README.md", "src/api/users.py", "db/migrations/001.sql",
                        "src/x.py", "Dockerfile"]) == ["tests", "docs", "api", "db", "other", "infra"]


def test_breaking_risk():
    assert breaking_risk([{"status": "removed"}], ["other"]) == "HIGH"
    assert breaking_risk([{"additions": 10}], ["db"]) == "HIGH"
    assert breaking_risk([{"additions": 10}], ["api"]) == "MEDIUM"
    assert breaking_risk([{"additions": 10}], ["tests"]) == "LOW"


def ollama(text=None, fail=False):
    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/api/generate" and body["stream"] is False
        if fail:
            return httpx.Response(500)
        return httpx.Response(200, json={"response": text})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_summary_from_ollama():
    s, by = run(summarise("http://o", "m", ["Add cache"], [{"filename": "a.py", "patch": "+x"}], ollama("Adds a cache.")))
    assert (s, by) == ("Adds a cache.", "m")


def test_summary_falls_back_to_messages():
    s, by = run(summarise("http://o", "m", ["Add cache\n\nlong body"], [], ollama(fail=True)))
    assert (s, by) == ("Add cache", "fallback")


class GH:
    async def compare(self, repo, base, head):
        return {"commits": [{"commit": {"message": "Use Redis", "author": {"name": "A", "date": "2026-10-29T08:00:00Z"}},
                             "author": {"login": "alice"}}],
                "files": [{"filename": "src/cache.py", "status": "modified", "additions": 5, "deletions": 2, "patch": "+r"},
                          {"filename": "old.py", "status": "removed", "additions": 0, "deletions": 9}]}

    async def commit(self, repo, sha):
        return {"commit": {"message": "First", "author": {"name": "A", "date": "2026-10-29T08:00:00Z"}},
                "author": {"login": "alice"}, "files": [{"filename": "a.py", "status": "added"}]}


def req(before="a" * 40, after="b" * 40):
    return CommitRequest(repo="o/r", ref="refs/heads/main", before=before, after=after)


def test_records_push_in_both_stores():
    db, driver = FakeDB(), FakeDriver()
    e = run(keep_commit(S, db, driver, GH(), req(), ollama("Moves cache to Redis.")))
    assert e.id == "bbbbbbb" and e.branch == "main" and e.authors == ["alice"]
    assert e.removed == ["old.py"] and e.breaking_risk == "HIGH" and e.summary_by == "llama3.2:3b"
    assert "bbbbbbb" in db.commit_ledger.docs
    assert "MERGE (c:Commit" in driver.calls[0][0]


def test_new_branch_uses_single_commit():
    e = run(keep_commit(S, FakeDB(), FakeDriver(), GH(), req(before=ZERO), ollama("x")))
    assert e.added == ["a.py"]


def test_deleted_branch_and_redelivery_skipped():
    db = FakeDB()
    assert run(keep_commit(S, db, FakeDriver(), GH(), req(after=ZERO), ollama("x"))) is None
    run(keep_commit(S, db, FakeDriver(), GH(), req(), ollama("x")))
    assert run(keep_commit(S, db, FakeDriver(), GH(), req(), ollama("x"))) is None


def test_graph_failure_marks_needs_sync():
    db = FakeDB()
    e = run(keep_commit(S, db, FakeDriver(fail=True), GH(), req(), ollama("x")))
    assert e.needs_sync and db.commit_ledger.docs["bbbbbbb"]["needs_sync"] is True
