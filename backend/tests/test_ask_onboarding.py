import asyncio
from types import SimpleNamespace

from app import ask as ask_mod
from app.claude import ClaudeResult
from app.models import DecisionIn
from app.onboarding import brief
from app.patterns import PatternIn, save_pattern
from app.reply_handler import ReplyRequest, handle_reply
from app.store import save_decision, supersede
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

S = SimpleNamespace(review_k=5, review_min_score=0.0)


def run(c):
    return asyncio.run(c)


def d(title, files, security=False, who="alice"):
    return DecisionIn(title=title, decision=f"Do {title}", reason="Because", future_implication=f"Always {title}",
                      source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"},
                      decided_by=[who], files=files, security_relevant=security)


class GH:
    def __init__(self):
        self.posted = []

    async def comment(self, r, n, b):
        self.posted.append(b)
        return {}


def req(text):
    return ReplyRequest(repo="o/r", number=3, author="carol", text=text)


def test_ask_cites_only_real_ids(monkeypatch):
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), d("fixed retries", ["src/api/retry.py"])))

    async def fake(settings, db, job, system, prompt, schema=None, urgent=False):
        assert job == "ask" and "Question: why no backoff?" in prompt
        return ClaudeResult(text="", data={"answer": "Thundering herd (KEEP-D-001).",
                                           "cited": ["KEEP-D-001", "KEEP-D-999"]}, model="haiku", mode="subscription")

    monkeypatch.setattr(ask_mod, "ask_claude", fake)
    gh = GH()
    driver = FakeDriver(records=[{"id": "KEEP-D-001", "score": 0.8}])
    out = run(handle_reply(S, db, driver, FakeEmbedder(), gh, req("keep: ask why no backoff?")))
    assert out.action == "ask" and out.targets == ["KEEP-D-001"]
    assert "Thundering herd" in gh.posted[0] and "KEEP-D-999" not in gh.posted[0]


def test_ask_with_empty_memory_skips_claude(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("Claude should not be called")

    monkeypatch.setattr(ask_mod, "ask_claude", boom)
    gh = GH()
    run(handle_reply(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, req("keep: ask anything?")))
    assert "Nothing in memory" in gh.posted[0]


def test_onboarding_brief_sections():
    db, drv, emb = FakeDB(), FakeDriver(), FakeEmbedder()
    run(save_decision(db, drv, emb, d("hash passwords with argon2", ["src/auth/users.py"], security=True)))
    run(save_decision(db, drv, emb, d("fixed retries", ["src/api/retry.py"], who="bob")))
    run(save_decision(db, drv, emb, d("backoff for batch", ["src/api/retry.py"], who="bob")))
    run(supersede(db, drv, "KEEP-D-002", "KEEP-D-003"))
    run(save_pattern(db, drv, PatternIn(rule="Use X | None", anti_pattern="Optional", language="python", reason="Modern typing")))
    db.commit_ledger.docs["abc1234"] = {"_id": "abc1234", "date": "2026-10-29", "breaking_risk": "LOW",
                                        "summary": "Updated README."}
    md = run(brief(db))
    assert "2 active decisions" in md
    assert "### Security rules" in md and "argon2" in md
    assert "`src` **KEEP-D-001**" in md
    assert "KEEP-P-001" in md and "@bob" in md
    assert "**KEEP-D-002** · fixed retries (superseded)" in md
    assert "`abc1234` (LOW) Updated README." in md
    assert md.endswith("— KEEP")


def test_onboard_command_posts_brief():
    gh = GH()
    out = run(handle_reply(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, req("keep: onboard")))
    assert out.action == "onboard" and "Memory is empty" in gh.posted[0]


def test_ask_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    async def fake_answer(settings, db, driver, embedder, q):
        assert q == "why fixed retries?"
        from app.models import Decision
        d = Decision(id="KEEP-D-001", **dec_in().model_dump())
        return "Thundering herd (KEEP-D-001).", [d]

    monkeypatch.setattr(main, "answer", fake_answer)
    main.app.state.settings = SimpleNamespace()
    main.app.state.db, main.app.state.neo4j, main.app.state.embedder = FakeDB(), FakeDriver(), FakeEmbedder()
    c = TestClient(main.app)
    r = c.post("/ask", json={"question": "  why fixed retries?  "})
    assert r.status_code == 200
    assert r.json() == {"answer": "Thundering herd (KEEP-D-001).",
                        "cited": [{"id": "KEEP-D-001", "title": "fixed retries", "status": "active"}]}
    assert c.post("/ask", json={"question": "a"}).status_code == 400


def dec_in():
    return d("fixed retries", ["src/api/retry.py"])
