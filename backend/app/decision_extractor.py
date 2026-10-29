"""Decision Extractor: when a PR merges, pull the decisions out of its discussion and store them.

Claude (Haiku) reads the PR text and comments and proposes decisions. Code enforces the rules:
files must be ones the PR changed, deciders must be people who took part, no near-duplicates,
one extraction per PR.
"""

from pydantic import BaseModel

from app.claude import ask_claude
from app.models import Confidence, DecisionIn, Origin
from app.search import SearchRequest, search_decisions
from app.store import save_decision

MAX_DISCUSSION_CHARS = 20_000

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "decision": {"type": "string"},
                    "rejected": {"type": "string"},
                    "reason": {"type": "string"},
                    "future_implication": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "decided_by": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["title", "decision", "reason", "future_implication", "files",
                             "decided_by", "confidence"],
            },
        }
    },
    "required": ["decisions"],
}

SYSTEM = """You extract engineering decisions from a merged pull request so a team remembers them.

A decision is a deliberate technical choice with a reason: picking one approach over another,
a rule future code must follow, a constraint someone explained.

Skip: "LGTM", thanks, unanswered questions, bot messages, style nitpicks, routine changes with no
stated reason. If the PR has no real decision, return an empty list. Most PRs have zero or one.
A choice and the rule that follows from it are ONE decision: put the rule in future_implication,
do not record it separately.

For each decision:
- title: under 8 words.
- decision: what was chosen. rejected: the alternative turned down (omit if none was discussed).
- reason: why, in the team's own terms. future_implication: the rule for future developers.
- files: only paths from the "Changed files" list that the decision governs.
- decided_by: GitHub logins of the people who made or agreed to it, only from "Participants".
- confidence: HIGH if the discussion states it plainly, MEDIUM if implied, LOW if guessed."""


class ExtractRequest(BaseModel):
    repo: str
    number: int
    post: bool = True


class ExtractOutcome(BaseModel):
    status: str  # recorded | none_found | skipped_not_merged | skipped_already_done
    recorded: list[str] = []
    skipped_duplicates: list[str] = []
    comment: str | None = None


def is_noise(c: dict) -> bool:
    body = (c.get("body") or "").strip()
    user = c.get("user") or {}
    return (user.get("type") == "Bot" or body.endswith("— KEEP") or len(body) < 4
            or body.lower() in {"lgtm", "lgtm!", "+1", "thanks", "thanks!", "ship it"})


def discussion_text(pr: dict, comments: list[dict]) -> tuple[str, set[str]]:
    author = (pr.get("user") or {}).get("login", "")
    people = {author} if author else set()
    lines = [f"PR author: @{author}", f"Title: {pr.get('title')}", f"Description:\n{pr.get('body') or '(none)'}", ""]
    for c in comments:
        if is_noise(c):
            continue
        login = (c.get("user") or {}).get("login", "?")
        people.add(login)
        where = f" on {c['path']}" if c.get("path") else ""
        lines.append(f"@{login}{where}: {c.get('body', '').strip()}")
    return "\n".join(lines)[:MAX_DISCUSSION_CHARS], people


def clean(raw: dict, changed: set[str], people: set[str], author: str, has_discussion: bool,
          source: dict | None = None) -> DecisionIn | None:
    files = [f for f in raw.get("files", []) if f in changed]
    deciders = [p.lstrip("@") for p in raw.get("decided_by", []) if p.lstrip("@") in people] or [author]
    confidence = raw.get("confidence", "MEDIUM")
    if confidence not in ("HIGH", "MEDIUM", "LOW"):
        confidence = "MEDIUM"
    if not has_discussion and confidence == "HIGH":
        confidence = "MEDIUM"  # code-only evidence never starts at HIGH (LORE rule)
    try:
        return DecisionIn(
            title=raw["title"][:120], decision=raw["decision"], rejected=raw.get("rejected") or None,
            reason=raw["reason"], future_implication=raw["future_implication"],
            source=source or {"type": "pr", "url": "https://github.com/unknown", "ref": "0"},
            decided_by=deciders, files=files, confidence=Confidence(confidence),
            origin=Origin.DISCUSSION if has_discussion else Origin.CODE,
        )
    except (KeyError, ValueError):
        return None


async def extract(settings, db, driver, embedder, gh, repo: str, number: int, post: bool = True) -> ExtractOutcome:
    key = f"{repo}#{number}"
    if await db.pr_extractions.find_one({"_id": key}):
        return ExtractOutcome(status="skipped_already_done")
    pr = await gh.pull(repo, number)
    if not pr.get("merged"):
        return ExtractOutcome(status="skipped_not_merged")

    files = await gh.pull_files(repo, number)
    changed = {f["filename"] for f in files}
    comments = await gh.issue_comments(repo, number) + await gh.review_comments(repo, number)
    text, people = discussion_text(pr, comments)
    author = (pr.get("user") or {}).get("login", "unknown")
    has_discussion = bool((pr.get("body") or "").strip()) or any(not is_noise(c) for c in comments)

    prompt = f"Participants: {', '.join(sorted(people))}\nChanged files:\n" + "\n".join(sorted(changed)) + f"\n\n{text}"
    result = await ask_claude(settings, db, "decision_extract", SYSTEM, prompt, schema=SCHEMA)

    recorded, dupes = [], []
    for raw in result.data.get("decisions", []):
        d = clean(raw, changed, people, author, has_discussion,
                  source={"type": "pr", "url": pr["html_url"], "ref": str(number)})
        if d is None:
            continue
        # Skip near-duplicates of something already in memory (fixed rule, in code).
        hits = await search_decisions(db, driver, embedder, SearchRequest(
            text=f"{d.title}\n{d.decision}\n{d.future_implication}", k=1, min_score=settings.duplicate_score))
        if hits:
            dupes.append(hits[0].decision.id)
            continue
        if not post:
            recorded.append(("(dry run)", d.title))  # dry run: show what would be saved, save nothing
            continue
        saved = await save_decision(db, driver, embedder, d)
        recorded.append((saved.id, saved.title))

    if post:
        await db.pr_extractions.update_one({"_id": key}, {"$set": {"recorded": [i for i, _ in recorded]}}, upsert=True)
    if not recorded:
        return ExtractOutcome(status="none_found", skipped_duplicates=dupes)

    lines = [f"**KEEP** recorded {len(recorded)} decision{'s' if len(recorded) > 1 else ''} from this PR:", ""]
    lines += [f"- **{i}** · {t}" for i, t in recorded]
    lines += ["", "Future PRs that go against these get flagged.", "", "— KEEP"]
    comment = "\n".join(lines)
    if post:
        await gh.comment(repo, number, comment)
    return ExtractOutcome(status="recorded", recorded=[i for i, _ in recorded], skipped_duplicates=dupes, comment=comment)
