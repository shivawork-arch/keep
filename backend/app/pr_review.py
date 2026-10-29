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
from app.patterns import Pattern, language_of, patterns_for
from app.review_layers import dependency_changes, linked_issues, security_hits
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
    promises: list[dict] = []
    security: list[dict] = []
    drift: list[dict] = []
    pattern_violations: list[dict] = []
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
        "promises": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "promise": {"type": "string"}, "status": {"type": "string", "enum": ["kept", "missing"]},
                "note": {"type": "string"}}, "required": ["promise", "status"]},
        },
        "security": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "file": {"type": "string"}, "line": {"type": "integer"}, "issue": {"type": "string"},
                "severity": {"type": "string", "enum": ["high", "medium", "low"]}},
                "required": ["file", "issue", "severity"]},
        },
        "drift": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "file": {"type": "string"}, "added": {"type": "string"}, "overlaps_with": {"type": "string"},
                "note": {"type": "string"}}, "required": ["file", "added", "note"]},
        },
        "pattern_violations": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "pattern_id": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"},
                "evidence": {"type": "string"}}, "required": ["pattern_id", "file"]},
        },
        "summary": {"type": "string"},
    },
    "required": ["conflicts", "promises", "security", "drift", "pattern_violations", "summary"],
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
- If nothing conflicts, return an empty conflicts list.

Other checks (each gets an empty list when its section is missing or nothing applies):
- promises: from "Linked issue", list what the developer said they would do; mark each kept or missing
  against the added lines. Skip vague chatter.
- security: only from "Security triggers"; keep the real problems, drop false alarms (test fixtures,
  placeholder values, safe usage). One short sentence each.
- drift: only from "Dependency changes"; flag a newly added library that does a job an existing listed
  library already does. Name the overlap. Ignore version bumps.
- pattern_violations: only for rules in "Pattern rules"; cite the rule ID and the offending line."""


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


def validate_layers(data: dict, paths: set[str], pattern_ids: set[str], has_issue: bool,
                    has_triggers: bool, has_deps: bool) -> dict:
    """Keep only what each layer was allowed to report, citing only files/rules we sent."""
    sev = {"high", "medium", "low"}
    return {
        "promises": [p for p in data.get("promises", []) if has_issue and p.get("status") in ("kept", "missing")
                     and p.get("promise")],
        "security": [x for x in data.get("security", []) if has_triggers and x.get("file") in paths
                     and x.get("severity") in sev and x.get("issue")],
        "drift": [x for x in data.get("drift", []) if has_deps and x.get("file") in paths and x.get("note")],
        "pattern_violations": [x for x in data.get("pattern_violations", [])
                               if x.get("pattern_id") in pattern_ids and x.get("file") in paths],
    }


# ---------- comment (code writes it, not Claude) ----------

REPLY_HELP = (
    "Reply on this PR:\n"
    "- `keep: intentional — <reason>` if this is on purpose (KEEP records the new decision)\n"
    "- `keep: accidental` if you'll fix the code\n"
    "- `keep: discuss` to ping the people who made the decision"
)


def build_comment(conflicts: list[Conflict], decisions: dict[str, Decision], summary: str,
                  files_reviewed: int, checked: int, layers: dict | None = None,
                  patterns: dict[str, Pattern] | None = None, issue: int | None = None) -> str:
    layers = layers or {}
    promises, security = layers.get("promises", []), layers.get("security", [])
    drift, violations = layers.get("drift", []), layers.get("pattern_violations", [])
    missing = [p for p in promises if p["status"] == "missing"]
    findings = len(conflicts) + len(missing) + len(security) + len(drift) + len(violations)

    if findings == 0:
        extra = f" All {len(promises)} promise(s) from issue {issue} kept." if promises else ""
        return (f"**KEEP:** No conflicts with past decisions. Checked {files_reviewed} file(s) "
                f"against {checked} related decision(s).{extra}\n\n— KEEP")

    lines = []
    if conflicts:
        n = len(conflicts)
        lines += [f"## KEEP: {n} conflict{'s' if n > 1 else ''} with past decisions", "", summary, ""]
    else:
        lines += ["## KEEP review", "", summary, ""]
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
                  f"**Decided by:** {', '.join('@' + p for p in d.decided_by)} in [{SOURCE_LABEL[d.source.type.value]} {d.source.ref}]({d.source.url})",
                  ""]
    if promises:
        lines += [f"### Promises from issue {issue}", "", "| Promise | Status |", "|---|---|"]
        lines += [f"| {p['promise']} | {'kept' if p['status'] == 'kept' else '**missing**'}"
                  f"{' — ' + p['note'] if p.get('note') else ''} |" for p in promises]
        lines.append("")
    if security:
        lines += ["### Security", ""]
        lines += [f"- **{x['severity']}** `{x['file']}`{' line ' + str(x['line']) if x.get('line') else ''}: {x['issue']}"
                  for x in security]
        lines.append("")
    if violations:
        patterns = patterns or {}
        lines += ["### Pattern rules", ""]
        for v in violations:
            p = patterns.get(v["pattern_id"])
            rule = f" · {p.rule}" if p else ""
            lines.append(f"- **{v['pattern_id']}**{rule}: `{v['file']}`"
                         f"{' line ' + str(v['line']) if v.get('line') else ''}"
                         f"{': `' + v['evidence'].strip() + '`' if v.get('evidence') else ''}")
        lines.append("")
    if drift:
        lines += ["### Tech drift", ""]
        lines += [f"- `{x['file']}` adds **{x['added']}**"
                  f"{' (overlaps with ' + x['overlaps_with'] + ')' if x.get('overlaps_with') else ''}: {x['note']}"
                  for x in drift]
        lines.append("")
    if conflicts:
        lines += [REPLY_HELP, ""]
    lines.append("— KEEP")
    return "\n".join(lines)


SOURCE_LABEL = {"pr": "PR", "issue": "issue", "comment": "comment", "commit": "commit"}

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

    # Layer inputs found by code.
    issue_numbers = linked_issues(pr.get("body") or "")
    issue_text, issue_no = "", None
    if issue_numbers:
        issue_no = issue_numbers[0]
        try:
            iss = await gh.issue(repo, issue_no)
            thread = await gh.issue_comments(repo, issue_no)
            issue_text = f"Issue {issue_no}: {iss.get('title')}\n{iss.get('body') or ''}\n" + "\n".join(
                f"@{(c.get('user') or {}).get('login')}: {c.get('body', '')}" for c in thread
                if (c.get("user") or {}).get("type") != "Bot")
        except Exception:
            issue_no = None
    triggers = security_hits(todo)
    deps = dependency_changes(todo)
    patterns = await patterns_for(db, {lang for f in todo if (lang := language_of(f.path))})

    if not (decisions or triggers or deps or patterns or issue_text):
        # Nothing for any layer to check: skip the Claude call entirely (cost rule).
        outcome = ReviewOutcome(status="skipped_no_memory", files_reviewed=paths, comment=NO_MEMORY_COMMENT)
    else:
        sections = [f"PR {number}: {pr.get('title')}\n{pr.get('body') or ''}"]
        if decisions:
            sections.append(f"## Decisions on record\n{decisions_text(decisions)}")
        if issue_text:
            sections.append(f"## Linked issue\n{issue_text[:6000]}")
        if triggers:
            sections.append("## Security triggers\n" + "\n".join(
                f"{t['file']}:{t['line']} [{t['trigger']}] {t['code']}" for t in triggers[:40]))
        if deps:
            sections.append("## Dependency changes\n" + "\n".join(
                f"{d['file']}: " + "; ".join(d["added"]) for d in deps))
        if patterns:
            sections.append("## Pattern rules\n" + "\n".join(
                f"[{p.id}] ({p.language}) {p.rule}. Anti-pattern: {p.anti_pattern}" for p in patterns))
        sections.append(f"## Added lines (new-file line numbers)\n{diff_text(todo)}")

        result = await ask_claude(settings, db, "pr_review", SYSTEM, "\n\n".join(sections),
                                  schema=REVIEW_SCHEMA, urgent=True)
        by_id = {d.id: d for d in decisions}
        by_pattern = {p.id: p for p in patterns}
        conflicts = validate_conflicts(result.data.get("conflicts", []), set(by_id), set(paths))
        layers = validate_layers(result.data, set(paths), set(by_pattern), bool(issue_text), bool(triggers), bool(deps))
        missing = [p for p in layers["promises"] if p["status"] == "missing"]
        found = conflicts or missing or layers["security"] or layers["drift"] or layers["pattern_violations"]
        outcome = ReviewOutcome(
            status="conflicts" if conflicts else ("findings" if found else "clean"),
            files_reviewed=paths, candidates=list(by_id), conflicts=conflicts, **layers,
            comment=build_comment(conflicts, by_id, result.data.get("summary", ""), len(paths), len(by_id),
                                  layers, by_pattern, issue_no),
        )

    if post:
        posted = await gh.comment(repo, number, outcome.comment)
        outcome.comment_url = posted.get("html_url")
        merged = {**prev, **{f.path: f.patch_hash for f in todo}}
        await db.pr_reviews.update_one(
            {"_id": key}, {"$set": {"files": [{"path": p, "hash": h} for p, h in merged.items()],
                                    "last_conflicts": [c.decision_id for c in outcome.conflicts]}},
            upsert=True)
    return outcome
