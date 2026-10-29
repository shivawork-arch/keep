import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import decision_extractor, incident_linker
from app.claude import BudgetExceeded, ClaudeResult
from app.incident_linker import IncidentRequest, link_incident
from app.migrate import MigrateRequest, migrate
from app.models import DecisionIn
from app.store import save_decision, supersede
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

S = SimpleNamespace(review_k=5, review_min_score=0.0, duplicate_score=0.97)
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def dec(title):
    return DecisionIn(title=title, decision=f"Use {title}", reason="Thundering herd", future_implication=f"Keep {title}",
                      source={"type": "pr", "url": "https://github.com/o/r/pull/1", "ref": "1"}, decided_by=["alice"])


class GH:
    def __init__(self, labels=("incident",)):
        self.labels, self.posted = labels, []

    async def issue(self, r, n):
        return {"title": "API melted under retry storm", "body": "Clients hammered us after a blip.",
                "html_url": f"https://github.com/{r}/issues/{n}", "labels": [{"name": l} for l in self.labels]}

    async def comment(self, r, n, b):
        self.posted.append(b)
        return {}


def patch_link(monkeypatch, links):
    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        assert job == "incident_link"
        return ClaudeResult(text="", data={"links": links, "summary": "Retry storm."}, model="haiku", mode="subscription")
    monkeypatch.setattr(incident_linker, "ask_claude", ask)


def test_incident_links_and_flags_recent_override(monkeypatch):
    db, drv, emb = FakeDB(), FakeDriver(), FakeEmbedder()
    run(save_decision(db, drv, emb, dec("fixed retries")))       # D-001
    run(save_decision(db, drv, emb, dec("backoff for batch")))   # D-002
    run(supersede(db, drv, "KEEP-D-001", "KEEP-D-002"))          # changed "now" (2026), within 30 days of real now
    patch_link(monkeypatch, [{"decision_id": "KEEP-D-001", "relation": "This is the herd it prevented."},
                             {"decision_id": "KEEP-D-404", "relation": "made up"}])
    driver = FakeDriver(records=[{"id": "KEEP-D-001", "score": 0.9}])
    gh = GH()
    out = run(link_incident(S, db, driver, emb, gh, IncidentRequest(repo="o/r", number=9)))
    assert out.status == "linked" and out.incident_id == "KEEP-I-001"
    assert [l["decision_id"] for l in out.links] == ["KEEP-D-001"]
    assert out.suspects == ["KEEP-D-001"]
    assert "KEEP-I-001" in gh.posted[0] and "changed recently" in gh.posted[0]
    assert any("MERGE (i:Incident" in q for q, _ in driver.calls)


def test_not_an_incident_is_skipped(monkeypatch):
    patch_link(monkeypatch, [])
    out = run(link_incident(S, FakeDB(), FakeDriver(), FakeEmbedder(), GH(labels=("bug",)),
                            IncidentRequest(repo="o/r", number=9)))
    assert out.status == "skipped_not_incident"


def test_incident_recorded_once_even_with_empty_memory(monkeypatch):
    patch_link(monkeypatch, [])
    db, gh = FakeDB(), GH()
    out = run(link_incident(S, db, FakeDriver(), FakeEmbedder(), gh, IncidentRequest(repo="o/r", number=9)))
    assert out.status == "no_links" and "Nothing in memory" in gh.posted[0]
    again = run(link_incident(S, db, FakeDriver(), FakeEmbedder(), gh, IncidentRequest(repo="o/r", number=9)))
    assert again.status == "skipped_already_done"


class MigGH:
    def __init__(self):
        self.posted = []

    async def merged_pulls(self, repo, limit):
        return [{"number": 3}, {"number": 2}]  # newest first, as GitHub returns

    async def pull(self, r, n):
        return {"merged": True, "html_url": f"https://github.com/{r}/pull/{n}", "title": f"PR {n}",
                "body": "We chose X because Y.", "user": {"login": "alice"}}

    async def pull_files(self, r, n):
        return [{"filename": "a.py"}]

    async def issue_comments(self, r, n):
        return []

    async def review_comments(self, r, n):
        return []

    async def comment(self, r, n, b):
        self.posted.append(n)
        return {}


def test_migrate_oldest_first_no_comments_and_rerun_safe(monkeypatch):
    seen = []

    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        seen.append(prompt.split("Title: ")[1].split("\n")[0])
        return ClaudeResult(text="", data={"decisions": [{
            "title": f"Decision {len(seen)}", "decision": f"Choose option {len(seen)}", "reason": "Because Y",
            "future_implication": "Keep it", "files": ["a.py"], "decided_by": ["alice"], "confidence": "HIGH"}]},
            model="haiku", mode="subscription")

    monkeypatch.setattr(decision_extractor, "ask_claude", ask)
    db, gh = FakeDB(), MigGH()
    out = run(migrate(S, db, FakeDriver(), FakeEmbedder(), gh, MigrateRequest(repo="o/r")))
    assert seen == ["PR 2", "PR 3"]  # oldest first
    assert out.recorded == {"2": ["KEEP-D-001"], "3": ["KEEP-D-002"]} and gh.posted == []
    again = run(migrate(S, db, FakeDriver(), FakeEmbedder(), gh, MigrateRequest(repo="o/r")))
    assert again.recorded == {} and len(seen) == 2


def test_migrate_stops_at_budget(monkeypatch):
    async def ask(*a, **k):
        raise BudgetExceeded("cap")

    monkeypatch.setattr(decision_extractor, "ask_claude", ask)
    out = run(migrate(S, FakeDB(), FakeDriver(), FakeEmbedder(), MigGH(), MigrateRequest(repo="o/r")))
    assert out.stopped_for_budget and out.scanned == 0
