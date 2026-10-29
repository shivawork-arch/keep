import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import decision_decay, guardrail_builder
from app.claude import ClaudeResult
from app.decision_decay import DecayRequest, decay
from app.guardrail_builder import GuardrailRequest, build_guardrails
from app.health_auditor import HealthRequest, audit, build_report
from app.models import DecisionIn
from app.reply_handler import ReplyRequest, handle_reply
from app.store import save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

S = SimpleNamespace(daily_call_cap=200, daily_token_cap=2_000_000)
NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def dec(title, files, confidence="HIGH"):
    return DecisionIn(title=title, decision=f"Use {title}", reason="Because of load", future_implication=f"Keep {title}",
                      source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"},
                      decided_by=["alice"], files=files, confidence=confidence)


def ledger(db, sha, files, days_after=1):
    db.commit_ledger.docs[sha] = {"_id": sha, "date": NOW - timedelta(days=days_after), "breaking_risk": "MEDIUM",
                                  "summary": f"Changed {files[0]}", "added": [], "modified": files, "removed": []}


def patch(monkeypatch, module, data):
    calls = []

    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        calls.append(prompt)
        return ClaudeResult(text="", data=data, model="haiku", mode="subscription")

    monkeypatch.setattr(module, "ask_claude", ask)
    return calls


# ---------- Decision Decay ----------

def test_decay_checks_busy_decisions_and_lowers_confidence(monkeypatch):
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), dec("fixed retries", ["src/retry.py"])))  # busy
    run(save_decision(db, FakeDriver(), FakeEmbedder(), dec("quiet rule", ["src/quiet.py"])))     # not busy
    for i in range(3):
        ledger(db, f"c{i}", ["src/retry.py"], days_after=i + 1)
    calls = patch(monkeypatch, decision_decay, {"verdict": "likely_outdated", "note": "Retry module was replaced."})
    out = run(decay(S, db, DecayRequest(), now=NOW))
    assert out.checked == {"KEEP-D-001": "likely_outdated"} and out.lowered == ["KEEP-D-001"]
    doc = db.decisions.docs["KEEP-D-001"]
    assert doc["confidence"] == "MEDIUM" and doc["status"] == "active"  # lowered, never auto-superseded
    assert "Changed src/retry.py" in calls[0]
    assert run(decay(S, db, DecayRequest(), now=NOW)).checked == {}  # rechecked only after 30 days


def test_decay_still_valid_keeps_confidence(monkeypatch):
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), dec("fixed retries", ["src/retry.py"])))
    for i in range(3):
        ledger(db, f"c{i}", ["src/retry.py"])
    patch(monkeypatch, decision_decay, {"verdict": "still_valid", "note": "Consistent."})
    run(decay(S, db, DecayRequest(), now=NOW))
    assert db.decisions.docs["KEEP-D-001"]["confidence"] == "HIGH"


# ---------- Health Auditor ----------

def test_health_report_sections():
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), dec("fixed retries", ["src/retry.py"], "LOW")))
    ledger(db, "c1", ["src/busy.py"])
    ledger(db, "c2", ["src/busy.py"])
    md = run(build_report(S, db, FakeDriver()))
    assert "| Active decisions | 1 |" in md
    assert "`src/busy.py`: changed in 2 commit(s)" in md
    assert "### Low confidence" in md and md.endswith("— KEEP")


class HealthGH:
    def __init__(self):
        self.created, self.updated = [], []

    async def create_issue(self, r, title, body, labels=None):
        self.created.append(title)
        return {"number": 42}

    async def update_issue(self, r, n, body):
        self.updated.append(n)
        return {}


def test_health_creates_issue_once_then_updates():
    db, gh = FakeDB(), HealthGH()
    assert run(audit(S, db, FakeDriver(), gh, HealthRequest(repo="o/r"))).issue_number == 42
    run(audit(S, db, FakeDriver(), gh, HealthRequest(repo="o/r")))
    assert gh.created == ["KEEP health report"] and gh.updated == [42]


# ---------- Guardrail Builder ----------

class GuardGH:
    def __init__(self, comments_per_pr):
        self.comments = comments_per_pr
        self.issues = []

    async def merged_pulls(self, r, limit):
        return [{"number": n, "user": {"login": "author"}} for n in self.comments]

    async def review_comments(self, r, n):
        return [{"user": {"login": "rev", "type": "User"}, "body": b, "path": "a.py"} for b in self.comments[n]]

    async def issue_comments(self, r, n):
        return []

    async def create_issue(self, r, title, body, labels=None):
        self.issues.append(body)
        return {"number": 77}


RULE = {"rule": "Use X | None instead of Optional[X]", "anti_pattern": "Optional[X]", "language": "python",
        "reason": "Modern typing", "bad_example": "-> Optional[int]", "good_example": "-> int | None",
        "people": ["rev"]}


def test_guardrail_needs_three_prs_and_proposes(monkeypatch):
    gh = GuardGH({1: ["Use int | None"], 2: ["Optional again"], 3: ["Please drop Optional"]})
    patch(monkeypatch, guardrail_builder, {"rules": [{**RULE, "pr_numbers": [1, 2, 3]},
                                                     {**RULE, "rule": "Two-PR rule", "pr_numbers": [1, 2]},
                                                     {**RULE, "rule": "Bad lang", "language": "cobol", "pr_numbers": [1, 2, 3]}]})
    db = FakeDB()
    out = run(build_guardrails(None, db, FakeDriver(), gh, GuardrailRequest(repo="o/r")))
    assert out.status == "proposed" and out.proposed == ["KEEP-P-001"]
    assert db.patterns.docs["KEEP-P-001"]["status"] == "proposed"
    assert "keep: approve" in gh.issues[0]


def test_guardrail_skips_with_too_little_feedback(monkeypatch):
    calls = patch(monkeypatch, guardrail_builder, {"rules": []})
    gh = GuardGH({1: ["one comment"], 2: ["LGTM"]})
    out = run(build_guardrails(None, FakeDB(), FakeDriver(), gh, GuardrailRequest(repo="o/r")))
    assert out.status == "skipped_too_little_feedback" and calls == []


def test_approve_command_activates_rule(monkeypatch):
    gh = GuardGH({1: ["Drop Optional here"], 2: ["Optional again"], 3: ["Use int | None"]})
    patch(monkeypatch, guardrail_builder, {"rules": [{**RULE, "pr_numbers": [1, 2, 3]}]})
    db = FakeDB()
    run(build_guardrails(None, db, FakeDriver(), gh, GuardrailRequest(repo="o/r")))

    class C:
        posted = []

        async def comment(self, r, n, b):
            self.posted.append(b)

    c = C()
    out = run(handle_reply(None, db, FakeDriver(), FakeEmbedder(), c,
                           ReplyRequest(repo="o/r", number=77, author="lead", text="keep: approve KEEP-P-001")))
    assert out.action == "approve" and db.patterns.docs["KEEP-P-001"]["status"] == "active"
    assert "now active" in c.posted[0]
