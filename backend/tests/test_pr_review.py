import asyncio
from types import SimpleNamespace

from app import pr_review
from app.claude import ClaudeResult
from app.models import DecisionIn
from app.pr_review import (added_lines, build_comment, changed_files, review_pr, validate_conflicts,
                           Conflict)
from app.store import save_decision
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

PATCH = """@@ -10,3 +10,4 @@ def fetch():
     tries = 0
-    delay = 1
+    delay = base * 2 ** attempt
+    jitter = 0
     return call()"""

RETRY = DecisionIn(
    title="Fixed retry intervals",
    decision="Use fixed retry intervals",
    rejected="Exponential backoff",
    reason="Thundering herd at 1000+ concurrent requests",
    future_implication="No exponential backoff in retry logic",
    source={"type": "pr", "url": "https://github.com/o/r/pull/42", "ref": "42"},
    decided_by=["alice"],
    files=["src/api/retry.py"],
)


def run(c):
    return asyncio.run(c)


# --- diff parsing ---

def test_added_lines_get_new_file_numbers():
    assert added_lines(PATCH) == [(11, "    delay = base * 2 ** attempt"), (12, "    jitter = 0")]


def test_changed_files_skips_noise():
    gh = [
        {"filename": "src/api/retry.py", "patch": PATCH, "status": "modified"},
        {"filename": "package-lock.json", "patch": PATCH, "status": "modified"},
        {"filename": "dist/app.min.js", "patch": PATCH, "status": "added"},
        {"filename": "old.py", "patch": PATCH, "status": "removed"},
        {"filename": "logo.png", "status": "added"},  # binary: no patch
        {"filename": "only_deletes.py", "patch": "@@ -1,2 +1,1 @@\n-a\n b", "status": "modified"},
    ]
    assert [f.path for f in changed_files(gh)] == ["src/api/retry.py"]


# --- validation and comment ---

def test_validate_drops_unknown_ids_and_files_and_sorts():
    raw = [
        {"decision_id": "KEEP-D-001", "file": "a.py", "explanation": "x", "severity": "low"},
        {"decision_id": "KEEP-D-999", "file": "a.py", "explanation": "made up", "severity": "high"},
        {"decision_id": "KEEP-D-002", "file": "not_in_diff.py", "explanation": "x", "severity": "high"},
        {"decision_id": "KEEP-D-002", "file": "a.py", "explanation": "x", "severity": "high"},
        {"decision_id": "KEEP-D-001", "file": "a.py", "severity": "high"},  # missing explanation
    ]
    out = validate_conflicts(raw, {"KEEP-D-001", "KEEP-D-002"}, {"a.py"})
    assert [(c.decision_id, c.severity) for c in out] == [("KEEP-D-002", "high"), ("KEEP-D-001", "low")]


def test_comment_has_ids_reply_commands_and_signature():
    from app.models import Decision
    d = Decision(id="KEEP-D-001", **RETRY.model_dump())
    c = Conflict(decision_id="KEEP-D-001", file="src/api/retry.py", line=11,
                 evidence="delay = base * 2 ** attempt", explanation="Doubles the delay.", severity="high")
    body = build_comment([c], {d.id: d}, "Backoff is back.", 1, 1)
    assert "KEEP-D-001 · Fixed retry intervals (high)" in body
    assert "`src/api/retry.py` line 11" in body
    assert "keep: intentional — <reason>" in body and "keep: accidental" in body and "keep: discuss" in body
    assert body.endswith("— KEEP")
    assert "#" + "1" not in body.replace("##", "")  # never '#' before a number


def test_clean_comment():
    assert "No conflicts" in build_comment([], {}, "", 2, 3)


# --- full agent with fakes ---

class FakeGH:
    def __init__(self, files):
        self.files = files
        self.comments = []

    async def pull(self, repo, number):
        return {"title": "Speed up retries", "body": "Retry faster under load"}

    async def pull_files(self, repo, number):
        return self.files

    async def comment(self, repo, number, body):
        self.comments.append(body)
        return {"html_url": f"https://github.com/{repo}/pull/{number}#c1"}


SETTINGS = SimpleNamespace(review_k=5, review_min_score=0.0)
FILES = [{"filename": "src/api/retry.py", "patch": PATCH, "status": "modified"}]


def seeded():
    db = FakeDB()
    run(save_decision(db, FakeDriver(), FakeEmbedder(), RETRY))
    return db


def patch_claude(monkeypatch, conflicts):
    calls = []

    async def fake_ask(settings, db, job, system, prompt, schema=None, urgent=False):
        calls.append({"job": job, "prompt": prompt, "urgent": urgent})
        return ClaudeResult(text="", data={"conflicts": conflicts, "summary": "Backoff is back."},
                            model="sonnet", mode="subscription")

    monkeypatch.setattr(pr_review, "ask_claude", fake_ask)
    return calls


CONFLICT = [{"decision_id": "KEEP-D-001", "file": "src/api/retry.py", "line": 11,
             "evidence": "delay = base * 2 ** attempt", "explanation": "Doubles the delay.", "severity": "high"}]


def test_conflict_found_and_posted(monkeypatch):
    calls = patch_claude(monkeypatch, CONFLICT)
    db, gh = seeded(), FakeGH(FILES)
    out = run(review_pr(SETTINGS, db, FakeDriver(), FakeEmbedder(), gh, "o/r", 7))
    assert out.status == "conflicts"
    assert [c.decision_id for c in out.conflicts] == ["KEEP-D-001"]
    assert len(gh.comments) == 1 and "KEEP-D-001" in gh.comments[0]
    assert calls[0]["job"] == "pr_review" and calls[0]["urgent"] is True
    assert "11:     delay = base * 2 ** attempt" in calls[0]["prompt"]


def test_found_by_file_path_even_without_vector_hit(monkeypatch):
    patch_claude(monkeypatch, [])
    db = seeded()
    # FakeDriver returns no vector hits, so the decision can only come from the file-path match.
    out = run(review_pr(SETTINGS, db, FakeDriver(), FakeEmbedder(), FakeGH(FILES), "o/r", 7))
    assert out.candidates == ["KEEP-D-001"]
    assert out.status == "clean"


def test_no_memory_skips_claude(monkeypatch):
    calls = patch_claude(monkeypatch, CONFLICT)
    gh = FakeGH(FILES)
    out = run(review_pr(SETTINGS, FakeDB(), FakeDriver(), FakeEmbedder(), gh, "o/r", 7))
    assert out.status == "skipped_no_memory"
    assert calls == []
    assert "Nothing in memory" in gh.comments[0]


def test_second_push_with_same_files_is_skipped(monkeypatch):
    calls = patch_claude(monkeypatch, [])
    db = seeded()
    run(review_pr(SETTINGS, db, FakeDriver(), FakeEmbedder(), FakeGH(FILES), "o/r", 7))
    out = run(review_pr(SETTINGS, db, FakeDriver(), FakeEmbedder(), FakeGH(FILES), "o/r", 7))
    assert out.status == "skipped_unchanged"
    assert len(calls) == 1


def test_dry_run_posts_nothing(monkeypatch):
    patch_claude(monkeypatch, CONFLICT)
    gh = FakeGH(FILES)
    out = run(review_pr(SETTINGS, seeded(), FakeDriver(), FakeEmbedder(), gh, "o/r", 7, post=False))
    assert out.status == "conflicts" and gh.comments == []
