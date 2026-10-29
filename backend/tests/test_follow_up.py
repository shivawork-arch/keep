import asyncio
from datetime import datetime, timedelta, timezone

from app.follow_up import follow_up
from app.models import DecisionIn
from app.reply_handler import ReplyRequest, handle_reply
from app.store import save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


class GH:
    def __init__(self, state="open"):
        self.state, self.posted = state, []

    async def pull(self, r, n):
        return {"state": self.state, "user": {"login": "dave"}}

    async def comment(self, r, n, b):
        self.posted.append((r, n, b))
        return {}


def db_with_review(hours_ago, conflicts=("KEEP-D-001",), resolved=False):
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), DecisionIn(
        title="Fixed retry intervals", decision="Use fixed intervals", reason="Thundering herd", future_implication="No backoff",
        source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"}, decided_by=["a"])))
    db.pr_reviews.docs["o/r#5"] = {"_id": "o/r#5", "last_conflicts": list(conflicts), "resolved": resolved,
                                   "followed_up": False, "reviewed_at": NOW - timedelta(hours=hours_ago)}
    return db


def test_nudges_old_unanswered_conflict_once():
    db, gh = db_with_review(72), GH()
    assert run(follow_up(db, gh, 48, now=NOW)).nudged == ["o/r#5"]
    repo, n, body = gh.posted[0]
    assert (repo, n) == ("o/r", 5) and "@dave" in body and "**KEEP-D-001** · Fixed retry intervals" in body
    assert run(follow_up(db, gh, 48, now=NOW)).nudged == []  # only once
    assert len(gh.posted) == 1


def test_too_recent_not_nudged():
    assert run(follow_up(db_with_review(10), GH(), 48, now=NOW)).nudged == []


def test_resolved_not_nudged():
    assert run(follow_up(db_with_review(72, resolved=True), GH(), 48, now=NOW)).nudged == []


def test_closed_pr_skipped_quietly():
    gh = GH(state="closed")
    out = run(follow_up(db_with_review(72), gh, 48, now=NOW))
    assert out.closed_skipped == ["o/r#5"] and gh.posted == []


def test_naive_datetime_from_mongo_handled():
    db = db_with_review(72)
    db.pr_reviews.docs["o/r#5"]["reviewed_at"] = (NOW - timedelta(hours=72)).replace(tzinfo=None)
    assert run(follow_up(db, GH(), 48, now=NOW)).nudged == ["o/r#5"]


def test_accidental_reply_resolves():
    db, gh = db_with_review(72), GH()
    run(handle_reply(None, db, FakeDriver(), FakeEmbedder(), gh,
                     ReplyRequest(repo="o/r", number=5, author="dave", text="keep: accidental")))
    assert db.pr_reviews.docs["o/r#5"]["resolved"] is True
    assert run(follow_up(db, GH(), 48, now=NOW)).nudged == []
