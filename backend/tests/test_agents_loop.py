"""Decision Extractor and Reply Handler, with fake GitHub, Claude and databases."""

import asyncio
from types import SimpleNamespace

import pytest

from app import decision_extractor, reply_handler
from app.claude import ClaudeError, ClaudeResult
from app.decision_extractor import clean, extract, is_noise
from app.models import DecisionIn, Status
from app.reply_handler import ReplyRequest, handle_reply, parse
from app.store import get_decision, link_decisions, save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

S = SimpleNamespace(duplicate_score=0.97)


def run(c):
    return asyncio.run(c)


class FakeGH:
    def __init__(self, merged=True, comments=None):
        self.merged, self.posted = merged, []
        self._comments = comments or []

    async def pull(self, repo, n):
        return {"merged": self.merged, "html_url": f"https://github.com/{repo}/pull/{n}",
                "title": "Cache user lookups", "body": "Use Redis, not an in-process dict.",
                "user": {"login": "alice"}}

    async def pull_files(self, repo, n):
        return [{"filename": "src/cache.py"}, {"filename": "README.md"}]

    async def issue_comments(self, repo, n):
        return self._comments

    async def review_comments(self, repo, n):
        return []

    async def comment(self, repo, n, body):
        self.posted.append(body)
        return {"html_url": "x"}


def fake_claude(monkeypatch, module, data):
    calls = []

    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        calls.append(job)
        if isinstance(data, Exception):
            raise data
        return ClaudeResult(text="", data=data, model="haiku", mode="subscription")

    monkeypatch.setattr(module, "ask_claude", ask)
    return calls


EXTRACTED = {"decisions": [{
    "title": "Redis for user cache", "decision": "Cache user lookups in Redis",
    "rejected": "In-process dict", "reason": "Workers do not share memory",
    "future_implication": "No module-level caches for shared data",
    "files": ["src/cache.py", "not/changed.py"], "decided_by": ["alice", "mallory"], "confidence": "HIGH"}]}


# ---------- Decision Extractor ----------

def test_noise_filter():
    assert is_noise({"body": "LGTM", "user": {"type": "User"}})
    assert is_noise({"body": "Real point here", "user": {"type": "Bot"}})
    assert is_noise({"body": "**KEEP:** ... \n\n— KEEP", "user": {"type": "User"}})
    assert not is_noise({"body": "Workers don't share memory", "user": {"type": "User"}})


def test_clean_keeps_only_changed_files_and_participants():
    d = clean(EXTRACTED["decisions"][0], {"src/cache.py"}, {"alice", "bob"}, "alice", True)
    assert d.files == ["src/cache.py"]
    assert d.decided_by == ["alice"]


def test_clean_code_only_caps_confidence():
    d = clean(EXTRACTED["decisions"][0], {"src/cache.py"}, {"alice"}, "alice", False)
    assert d.confidence.value == "MEDIUM" and d.origin.value == "code"


def test_extract_records_and_comments(monkeypatch):
    calls = fake_claude(monkeypatch, decision_extractor, EXTRACTED)
    db, gh = FakeDB(), FakeGH()
    out = run(extract(S, db, FakeDriver(), FakeEmbedder(), gh, "o/r", 9))
    assert out.status == "recorded" and out.recorded == ["KEEP-D-001"]
    d = run(get_decision(db, "KEEP-D-001"))
    assert str(d.source.url) == "https://github.com/o/r/pull/9" and d.source.ref == "9"
    assert calls == ["decision_extract"]
    assert "KEEP-D-001" in gh.posted[0] and gh.posted[0].endswith("— KEEP")


def test_extract_only_once_per_pr(monkeypatch):
    fake_claude(monkeypatch, decision_extractor, EXTRACTED)
    db = FakeDB()
    run(extract(S, db, FakeDriver(), FakeEmbedder(), FakeGH(), "o/r", 9))
    assert run(extract(S, db, FakeDriver(), FakeEmbedder(), FakeGH(), "o/r", 9)).status == "skipped_already_done"


def test_extract_skips_unmerged(monkeypatch):
    calls = fake_claude(monkeypatch, decision_extractor, EXTRACTED)
    out = run(extract(S, FakeDB(), FakeDriver(), FakeEmbedder(), FakeGH(merged=False), "o/r", 9))
    assert out.status == "skipped_not_merged" and calls == []


def test_extract_skips_duplicates(monkeypatch):
    fake_claude(monkeypatch, decision_extractor, EXTRACTED)
    db = FakeDB()
    run(extract(S, db, FakeDriver(), FakeEmbedder(), FakeGH(), "o/r", 9))
    # The graph says the new decision is 0.99 similar to KEEP-D-001.
    driver = FakeDriver(records=[{"id": "KEEP-D-001", "score": 0.99}])
    out = run(extract(S, db, driver, FakeEmbedder(), FakeGH(), "o/r", 10))
    assert out.status == "none_found" and out.skipped_duplicates == ["KEEP-D-001"]


def test_extract_nothing_found_posts_nothing(monkeypatch):
    fake_claude(monkeypatch, decision_extractor, {"decisions": []})
    gh = FakeGH()
    assert run(extract(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, "o/r", 9)).status == "none_found"
    assert gh.posted == []


# ---------- Reply Handler ----------

RETRY = DecisionIn(title="Fixed retry intervals", decision="Use fixed retry intervals",
                   rejected="Exponential backoff", reason="Thundering herd",
                   future_implication="No exponential backoff in retry logic",
                   source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"},
                   decided_by=["alice", "bob"], files=["src/api/retry.py"])


def flagged_db():
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), RETRY))
    db.pr_reviews.docs["o/r#5"] = {"_id": "o/r#5", "last_conflicts": ["KEEP-D-001"]}
    return db


def req(text, author="carol"):
    return ReplyRequest(repo="o/r", number=5, comment_id=77, author=author, text=text)


@pytest.mark.parametrize("text,expected", [
    ("keep: intentional — batch jobs only, no live traffic", ("intentional", [], "batch jobs only, no live traffic")),
    ("keep: intentional - quick reason", ("intentional", [], "quick reason")),
    ("KEEP: Intentional KEEP-D-002 — why", ("intentional", ["KEEP-D-002"], "why")),
    ("keep: accidental", ("accidental", [], "")),
    ("keep: discuss KEEP-D-001", ("discuss", ["KEEP-D-001"], "")),
    ("keep: maybe", (None, [], "")),
    ("thanks keep", (None, [], "")),
])
def test_parse(text, expected):
    assert parse(text) == expected


def test_accidental_keeps_decision(monkeypatch):
    gh = FakeGH()
    out = run(handle_reply(S, flagged_db(), FakeDriver(), FakeEmbedder(), gh, req("keep: accidental")))
    assert out.action == "accidental" and out.targets == ["KEEP-D-001"]
    assert "KEEP-D-001 stands" in gh.posted[0]


def test_discuss_pings_deciders_not_author():
    gh = FakeGH()
    out = run(handle_reply(S, flagged_db(), FakeDriver(), FakeEmbedder(), gh, req("keep: discuss", author="bob")))
    assert out.action == "discuss"
    assert "@alice" in gh.posted[0] and "@bob wants" in gh.posted[0]


def test_intentional_needs_reason():
    out = run(handle_reply(S, flagged_db(), FakeDriver(), FakeEmbedder(), FakeGH(), req("keep: intentional")))
    assert out.action == "needs_reason"


def test_intentional_overrides_and_supersedes(monkeypatch):
    fake_claude(monkeypatch, reply_handler, {"title": "Backoff allowed for batch jobs",
                                             "decision": "Exponential backoff is allowed in batch jobs",
                                             "future_implication": "Live traffic still uses fixed intervals"})
    db, driver, gh = flagged_db(), FakeDriver(), FakeGH()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), RETRY))          # KEEP-D-002
    run(link_decisions(db, FakeDriver(), "KEEP-D-002", "KEEP-D-001"))    # D-002 depends on D-001
    out = run(handle_reply(S, db, driver, FakeEmbedder(), gh,
                           req("keep: intentional — batch jobs only, no live traffic")))
    assert out.action == "intentional" and out.created == ["KEEP-D-003"]
    old, new = run(get_decision(db, "KEEP-D-001")), run(get_decision(db, "KEEP-D-003"))
    assert old.status == Status.SUPERSEDED
    assert new.rejected == "Use fixed retry intervals" and new.decided_by == ["carol"]
    assert new.reason == "batch jobs only, no live traffic"
    assert "KEEP-D-003" in run(get_decision(db, "KEEP-D-002")).depends_on  # link moved to the new one
    assert any("SUPERSEDES" in q for q, _ in driver.calls)
    assert "replaces **KEEP-D-001**" in gh.posted[0]


def test_intentional_falls_back_if_claude_fails(monkeypatch):
    fake_claude(monkeypatch, reply_handler, ClaudeError("down"))
    db = flagged_db()
    out = run(handle_reply(S, db, FakeDriver(), FakeEmbedder(), FakeGH(), req("keep: intentional — ok here")))
    assert out.created == ["KEEP-D-002"]
    assert run(get_decision(db, "KEEP-D-002")).title == "Exception to KEEP-D-001"


def test_no_flagged_decision():
    out = run(handle_reply(S, FakeDB(), FakeDriver(), FakeEmbedder(), FakeGH(), req("keep: accidental")))
    assert out.action == "no_target"


def test_unknown_command_gets_help():
    gh = FakeGH()
    out = run(handle_reply(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, req("keep: whatever")))
    assert out.action == "unknown" and "keep: intentional" in gh.posted[0]


def test_dry_run_saves_nothing(monkeypatch):
    fake_claude(monkeypatch, decision_extractor, EXTRACTED)
    db, gh = FakeDB(), FakeGH()
    out = run(extract(S, db, FakeDriver(), FakeEmbedder(), gh, "o/r", 9, post=False))
    assert out.status == "recorded" and db.decisions.docs == {} and gh.posted == []
