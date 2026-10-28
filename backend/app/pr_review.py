"""PR Review agent, memory-conflict layer.

Code does the fixed work: fetch the diff, skip noise, find related decisions (by meaning AND by
file path), skip unchanged files, build and post the comment. Claude only judges conflicts.
One Claude call per review; structured JSON back.
"""

import hashlib
import re

from pydantic import BaseModel

from app.claude import ask_claude
from app.models import Decision
from app.search import SearchRequest, search_decisions
from app.store import from_mongo

SKIP_FILES = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|uv\.lock|"
    r"Cargo\.lock|go\.sum|composer\.lock)$|\.min\.(js|css)$|(^|/)(dist|build|vendor|node_modules)/|"
    r"\.(png|jpe?g|gif|svg|ico|pdf|lock)$"
)
MAX_DIFF_CHARS = 30_000
MAX_SEARCH_CHARS = 4_000
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class ChangedFile(BaseModel):
    path: str
    added: list[tuple[int, str]]  # (new line number, text)
    patch_hash: str


class Conflict(BaseModel):
    decision_id: str
    file: str
    line: int | None = None
    evidence: str | None = None
    explanation: str
    severity: str


class ReviewOutcome(BaseModel):
    status: str  # conflicts | clean | skipped_no_memory | skipped_unchanged | skipped_no_code
    files_reviewed: list[str] = []
    candidates: list[str] = []
    conflicts: list[Conflict] = []
    comment: str | None = None
    comment_url: str | None = None


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision_id": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "evidence": {"type": "string"},
                    "explanation": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["decision_id", "file", "explanation", "severity"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["conflicts", "summary"],
}

SYSTEM = """You review pull requests against a team's recorded engineering decisions.
You are an experienced engineer who has seen these exact things break. Direct, a bit dry, specific.

Rules:
- Only report a conflict when the added code clearly goes against a listed decision's rule.
  Same idea in different words counts (e.g. "progressive delay with multiplier" = exponential backoff,
  "module-level dict" = in-memory cache, "stored without bcrypt/argon2" = plaintext password).
- Be conservative. A false alarm is worse than a miss. If unsure, leave it out.
- Only use decision IDs from the list. Only use file paths from the diff.
- "line" is the new-file line number shown in the diff. "evidence" is that code line, copied exactly.
- "explanation": 1-3 sentences on what the code does and why it breaks that decision. No generic advice.
- "summary": one dry sentence for the whole PR.
- If nothing conflicts, return an empty conflicts list."""


# ---------- diff handling (pure, tested) ----------

def added_lines(patch: str) -> list[tuple[int, str]]:
    """New-file line numbers and text of added lines in a unified diff patch."""
    out, line = [], None
    for raw in patch.splitlines():
        m = HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            out.append((line, raw[1:]))
            line += 1
        elif raw.startswith("-"):
            continue
        else:
            line += 1
    return out


def changed_files(gh_files: list[dict]) -> list[ChangedFile]:
    result = []
    for f in gh_files:
        path, patch = f.get("filename", ""), f.get("patch")
        if not patch or f.get("status") == "removed" or SKIP_FILES.search(path):
            continue
        added = added_lines(patch)
        if added:
            result.append(ChangedFile(path=path, added=added,
                                      patch_hash=hashlib.sha256(patch.encode()).hexdigest()))
    return result


def diff_text(files: list[ChangedFile]) -> str:
    parts = []
    for f in files:
        parts.append(f"### {f.path}\n" + "\n".join(f"{n}: {t}" for n, t in f.added))
    text = "\n\n".join(parts)
    return text[:MAX_DIFF_CHARS] + ("\n[diff truncated]" if len(text) > MAX_DIFF_CHARS else "")


def decisions_text(decisions: list[Decision]) -> str:
    blocks = []
    for d in decisions:
        blocks.append("\n".join(filter(None, [
            f"[{d.id}] {d.title}",
            f"Decision: {d.decision}",
            f"Rejected: {d.rejected}" if d.rejected else None,
            f"Reason: {d.reason}",
            f"Rule: {d.future_implication}",
            f"Files: {', '.join(d.files)}" if d.files else None,
        ])))
    return "\n\n".join(blocks)


def validate_conflicts(raw: list[dict], decision_ids: set[str], paths: set[str]) -> list[Conflict]:
    """Drop anything that cites a decision or file we did not give Claude (fixed rule, in code)."""
    out = []
    for c in raw:
        try:
            conflict = Conflict(**c)
        except Exception:
            continue
        if conflict.decision_id in decision_ids and conflict.file in paths \
                and conflict.severity in ("high", "medium", "low"):
            out.append(conflict)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda c: order[c.severity])


# ---------- comment (code writes it, not Claude) ----------

REPLY_HELP = (
    "Reply on this PR:\n"
    "- `keep: intentional — <reason>` if this is on purpose (KEEP records the new decision)\n"
    "- `keep: accidental` if you'll fix the code\n"
    "- `keep: discuss` to ping the people who made the decision"
)


def build_comment(conflicts: list[Conflict], decisions: dict[str, Decision], summary: str,
                  files_reviewed: int, checked: int) -> str:
    if not conflicts:
        return (f"**KEEP:** No conflicts with past decisions. Checked {files_reviewed} file(s) "
                f"against {checked} related decision(s).\n\n— KEEP")
    n = len(conflicts)
    lines = [f"## KEEP: {n} conflict{'s' if n > 1 else ''} with past decisions", "", summary, ""]
    for c in conflicts:
        d = decisions[c.decision_id]
        where = f"`{c.file}`" + (f" line {c.line}" if c.line else "")
        lines += [f"### {d.id} · {d.title} ({c.severity})", where]
        if c.evidence:
            lines.append(f"```\n{c.evidence.strip()}\n```")
        lines += [c.explanation, "",
                  f"**Decision:** {d.decision}  ",
                  *( [f"**Rejected:** {d.rejected}  "] if d.rejected else [] ),
                  f"**Why:** {d.reason}  ",
                  f"**Decided by:** {', '.join('@' + p for p in d.decided_by)} in [{d.source.type.value} {d.source.ref}]({d.source.url})",
                  ""]
    lines += [REPLY_HELP, "", "— KEEP"]
    return "\n".join(lines)


NO_MEMORY_COMMENT = "**KEEP:** Nothing in memory touches this change. Skipped the deep review.\n\n— KEEP"


# ---------- the agent ----------

async def related_decisions(db, driver, embedder, settings, query: str, paths: list[str]) -> list[Decision]:
    """Union of: decisions found by meaning, and decisions that govern the changed files."""
    found: dict[str, Decision] = {}
    hits = await search_decisions(db, driver, embedder, SearchRequest(
        text=query[:MAX_SEARCH_CHARS], k=settings.review_k, min_score=settings.review_min_score))
    for h in hits:
        found[h.decision.id] = h.decision
    async for doc in db.decisions.find({"files": {"$in": paths}, "status": "active"}):
        d = from_mongo(doc)
        found.setdefault(d.id, d)
    return list(found.values())


async def review_pr(settings, db, driver, embedder, gh, repo: str, number: int,
                    post: bool = True) -> ReviewOutcome:
    pr = await gh.pull(repo, number)
    files = changed_files(await gh.pull_files(repo, number))
    if not files:
        return ReviewOutcome(status="skipped_no_code")

    # Re-review only files whose content changed since the last review (cost rule).
    key = f"{repo}#{number}"
    # Stored as a list of {path, hash}: file paths contain dots, which make poor Mongo field names.
    stored = (await db.pr_reviews.find_one({"_id": key}) or {}).get("files", [])
    prev = {f["path"]: f["hash"] for f in stored}
    todo = [f for f in files if prev.get(f.path) != f.patch_hash]
    if not todo:
        return ReviewOutcome(status="skipped_unchanged")
    paths = [f.path for f in todo]

    query = "\n".join(filter(None, [pr.get("title"), pr.get("body") or "",
                                    "\n".join(t for f in todo for _, t in f.added)]))
    decisions = await related_decisions(db, driver, embedder, settings, query, paths)

    if not decisions:
        # No related memory: skip the Claude call entirely (cost rule).
        outcome = ReviewOutcome(status="skipped_no_memory", files_reviewed=paths, comment=NO_MEMORY_COMMENT)
    else:
        prompt = (f"PR #{number}: {pr.get('title')}\n{pr.get('body') or ''}\n\n"
                  f"## Decisions on record\n{decisions_text(decisions)}\n\n"
                  f"## Added lines (new-file line numbers)\n{diff_text(todo)}")
        result = await ask_claude(settings, db, "pr_review", SYSTEM, prompt, schema=REVIEW_SCHEMA, urgent=True)
        by_id = {d.id: d for d in decisions}
        conflicts = validate_conflicts(result.data.get("conflicts", []), set(by_id), set(paths))
        outcome = ReviewOutcome(
            status="conflicts" if conflicts else "clean",
            files_reviewed=paths, candidates=list(by_id), conflicts=conflicts,
            comment=build_comment(conflicts, by_id, result.data.get("summary", ""), len(paths), len(by_id)),
        )

    if post:
        posted = await gh.comment(repo, number, outcome.comment)
        outcome.comment_url = posted.get("html_url")
        merged = {**prev, **{f.path: f.patch_hash for f in todo}}
        await db.pr_reviews.update_one(
            {"_id": key}, {"$set": {"files": [{"path": p, "hash": h} for p, h in merged.items()]}}, upsert=True)
    return outcome
