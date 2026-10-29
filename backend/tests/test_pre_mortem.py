import asyncio
from types import SimpleNamespace

from app import pre_mortem as pm
from app.claude import ClaudeResult
from app.models import DecisionIn
from app.pre_mortem import pre_mortem
from app.store import save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

S = SimpleNamespace(review_k=5, review_min_score=0.0)
CACHE = DecisionIn(title="Redis for shared cache", decision="Cache shared data in Redis",
                   rejected="Module-level dict", reason="Workers don't share memory",
                   future_implication="No in-process caches for cross-request data",
                   source={"type": "pr", "url": "https://github.com/o/r/pull/6", "ref": "6"},
                   decided_by=["alice"], files=["src/cache.py"])


def run(c):
    return asyncio.run(c)


class GH:
    def __init__(self, is_pr=False):
        self.is_pr, self.posted = is_pr, []

    async def issue(self, r, n):
        i = {"title": "Cache product prices", "body": "Prices are slow; keep them in a dict."}
        if self.is_pr:
            i["pull_request"] = {}
        return i

    async def comment(self, r, n, b):
        self.posted.append(b)
        return {"html_url": "u"}


def seeded():
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), CACHE))
    return db


HIT = FakeDriver(records=[{"id": "KEEP-D-001", "score": 0.8}])


def patch(monkeypatch, data):
    calls = []

    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        calls.append(job)
        return ClaudeResult(text="", data=data, model="sonnet", mode="subscription")

    monkeypatch.setattr(pm, "ask_claude", ask)
    return calls


def test_warns_with_risks_and_checklist(monkeypatch):
    calls = patch(monkeypatch, {"risks": [{"decision_id": "KEEP-D-001", "risk": "A dict per worker again.",
                                           "advice": "Use Redis with a TTL."},
                                          {"decision_id": "KEEP-D-404", "risk": "x", "advice": "y"}],
                                "checklist": ["Cache lives in Redis"], "summary": "Seen this one before."})
    gh = GH()
    out = run(pre_mortem(S, seeded(), HIT, FakeEmbedder(), gh, "o/r", 8))
    assert out.status == "warned" and [r["decision_id"] for r in out.risks] == ["KEEP-D-001"]
    assert calls == ["pre_mortem"]
    body = gh.posted[0]
    assert "## KEEP pre-mortem" in body and "KEEP-D-001 · Redis for shared cache" in body
    assert "- [ ] Cache lives in Redis" in body and body.endswith("— KEEP")


def test_no_memory_no_claude_no_comment(monkeypatch):
    calls = patch(monkeypatch, {"risks": [], "checklist": [], "summary": ""})
    gh = GH()
    out = run(pre_mortem(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, "o/r", 8))
    assert out.status == "skipped_no_memory" and calls == [] and gh.posted == []


def test_nothing_relevant_posts_nothing(monkeypatch):
    patch(monkeypatch, {"risks": [], "checklist": [], "summary": "Fine."})
    gh = GH()
    assert run(pre_mortem(S, seeded(), HIT, FakeEmbedder(), gh, "o/r", 8)).status == "nothing_relevant"
    assert gh.posted == []


def test_skips_pull_requests(monkeypatch):
    calls = patch(monkeypatch, {})
    out = run(pre_mortem(S, seeded(), HIT, FakeEmbedder(), GH(is_pr=True), "o/r", 8))
    assert out.status == "skipped_pull_request" and calls == []
