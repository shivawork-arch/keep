import asyncio
from types import SimpleNamespace

from app import pr_review
from app.claude import ClaudeResult
from app.patterns import PatternIn, language_of, patterns_for, save_pattern
from app.pr_review import ChangedFile, build_comment, review_pr, validate_layers
from app.review_layers import dependency_changes, linked_issues, security_hits
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder


def run(c):
    return asyncio.run(c)


def cf(path, lines):
    return ChangedFile(path=path, added=list(enumerate(lines, 1)), patch_hash="h")


def test_linked_issues():
    assert linked_issues("Fixes #12, also closes #7 and fixes #12") == [12, 7]
    assert linked_issues("see #3") == []


def test_security_triggers():
    f = cf("app.py", ['API_KEY = "sk-live-abc123"', "subprocess.run(cmd, shell=True)",
                      "requests.get(u, verify=False)", 'cur.execute(f"SELECT * FROM u WHERE id={uid}")',
                      "h = hashlib.md5(x)", "data = pickle.loads(blob)", "x = 1 + 2"])
    found = {h["trigger"] for h in security_hits([f])}
    assert found == {"hardcoded secret", "shell command", "TLS check off", "SQL from strings",
                     "weak hash", "unsafe deserialise"}


def test_dependency_changes_only_dep_files():
    files = [cf("requirements.txt", ["httpx==0.28", "requests==2.32"]), cf("app.py", ["import x"])]
    assert dependency_changes(files) == [{"file": "requirements.txt", "added": ["httpx==0.28", "requests==2.32"]}]


def test_language_of():
    assert language_of("src/a.py") == "python" and language_of("README.md") is None


def test_patterns_saved_and_found_by_language():
    db, driver = FakeDB(), FakeDriver()
    p = run(save_pattern(db, driver, PatternIn(rule="Use X | None", anti_pattern="Optional[X]",
                                               language="python", reason="Modern typing")))
    assert p.id == "KEEP-P-001"
    assert "MERGE (p:Pattern" in driver.calls[0][0]
    assert [x.id for x in run(patterns_for(db, {"python"}))] == ["KEEP-P-001"]
    assert run(patterns_for(db, {"go"})) == []


def test_validate_layers_only_allowed_findings():
    data = {"promises": [{"promise": "add tests", "status": "missing"}],
            "security": [{"file": "a.py", "issue": "secret", "severity": "high"},
                         {"file": "z.py", "issue": "made up", "severity": "high"}],
            "drift": [{"file": "requirements.txt", "added": "requests", "note": "httpx does this"}],
            "pattern_violations": [{"pattern_id": "KEEP-P-001", "file": "a.py"},
                                   {"pattern_id": "KEEP-P-999", "file": "a.py"}]}
    out = validate_layers(data, {"a.py", "requirements.txt"}, {"KEEP-P-001"}, has_issue=False,
                          has_triggers=True, has_deps=True)
    assert out["promises"] == []  # no linked issue was given
    assert [x["file"] for x in out["security"]] == ["a.py"]
    assert len(out["drift"]) == 1
    assert [x["pattern_id"] for x in out["pattern_violations"]] == ["KEEP-P-001"]


def test_comment_sections():
    layers = {"promises": [{"promise": "Add retry tests", "status": "missing"}],
              "security": [{"file": "a.py", "line": 3, "issue": "Hardcoded API key", "severity": "high"}],
              "drift": [{"file": "requirements.txt", "added": "requests", "overlaps_with": "httpx",
                         "note": "Two HTTP clients"}],
              "pattern_violations": []}
    body = build_comment([], {}, "Mostly fine.", 2, 0, layers, {}, issue=12)
    assert "## KEEP review" in body and "Promises from issue 12" in body
    assert "**missing**" in body and "Hardcoded API key" in body and "overlaps with httpx" in body
    assert "keep: intentional" not in body  # reply commands only for decision conflicts
    assert "#12" not in body


class GH:
    def __init__(self, body, files):
        self.body, self.files, self.comments = body, files, []

    async def pull(self, r, n):
        return {"title": "Add client", "body": self.body}

    async def pull_files(self, r, n):
        return self.files

    async def issue(self, r, n):
        return {"title": "Retry client", "body": "Please add tests and a timeout."}

    async def issue_comments(self, r, n):
        return [{"user": {"login": "dev", "type": "User"}, "body": "I'll add tests and a 5s timeout."}]

    async def comment(self, r, n, b):
        self.comments.append(b)
        return {"html_url": "u"}


def fake_claude(monkeypatch, data):
    prompts = []

    async def ask(settings, db, job, system, prompt, schema=None, urgent=False):
        prompts.append(prompt)
        return ClaudeResult(text="", data=data, model="sonnet", mode="subscription")

    monkeypatch.setattr(pr_review, "ask_claude", ask)
    return prompts


S = SimpleNamespace(review_k=5, review_min_score=0.0)
SECRET_PATCH = '@@ -0,0 +1,2 @@\n+TOKEN = "abcd1234secret"\n+print(TOKEN)'


def test_security_trigger_alone_makes_claude_run(monkeypatch):
    prompts = fake_claude(monkeypatch, {"conflicts": [], "promises": [], "drift": [], "pattern_violations": [],
                                        "security": [{"file": "a.py", "line": 1, "issue": "Hardcoded token",
                                                      "severity": "high"}], "summary": "One leak."})
    gh = GH("No link here", [{"filename": "a.py", "patch": SECRET_PATCH, "status": "added"}])
    out = run(review_pr(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, "o/r", 3))
    assert out.status == "findings" and out.security[0]["issue"] == "Hardcoded token"
    assert "## Security triggers" in prompts[0] and "## Decisions on record" not in prompts[0]
    assert "Hardcoded token" in gh.comments[0]


def test_linked_issue_sends_promises(monkeypatch):
    prompts = fake_claude(monkeypatch, {"conflicts": [], "security": [], "drift": [], "pattern_violations": [],
                                        "promises": [{"promise": "add tests", "status": "missing"},
                                                     {"promise": "5s timeout", "status": "kept"}],
                                        "summary": "Half done."})
    gh = GH("Fixes #12", [{"filename": "client.py", "patch": "@@ -0,0 +1 @@\n+timeout = 5", "status": "added"}])
    out = run(review_pr(S, FakeDB(), FakeDriver(), FakeEmbedder(), gh, "o/r", 3))
    assert "## Linked issue" in prompts[0] and "I'll add tests" in prompts[0]
    assert out.status == "findings" and "Promises from issue 12" in gh.comments[0]
