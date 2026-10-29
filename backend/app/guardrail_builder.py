"""Guardrail Builder (scheduled): turn feedback that keeps repeating across PRs into pattern rules.

Code gathers human review comments from recent merged PRs and enforces the rule that a point must
appear in at least 3 different PRs. Claude (Haiku) groups the feedback and drafts the rule.
New rules start as "proposed" and go live only after `keep: approve KEEP-P-00x`.
"""

from pydantic import BaseModel

from app.claude import ask_claude
from app.decision_extractor import is_noise
from app.patterns import LANGUAGES, PatternIn, save_pattern
from app.state import get_state, set_state

MIN_PRS = 3

SCHEMA = {
    "type": "object",
    "properties": {"rules": {"type": "array", "items": {"type": "object", "properties": {
        "rule": {"type": "string"}, "anti_pattern": {"type": "string"},
        "language": {"type": "string"}, "reason": {"type": "string"},
        "bad_example": {"type": "string"}, "good_example": {"type": "string"},
        "pr_numbers": {"type": "array", "items": {"type": "integer"}},
        "people": {"type": "array", "items": {"type": "string"}}},
        "required": ["rule", "anti_pattern", "language", "reason", "pr_numbers"]}}},
    "required": ["rules"],
}

SYSTEM = f"""You read code review comments from several pull requests and find feedback that keeps coming back.
Propose a coding rule ONLY when the same point was made in at least {MIN_PRS} different PRs.
Each rule: rule (one line), anti_pattern (what reviewers keep flagging), language (lowercase, e.g. python),
reason, bad_example and good_example (one line of code each), pr_numbers (where it came up),
people (reviewers who raised it). Skip one-off comments, taste, and anything already in "Existing rules"."""


class GuardrailRequest(BaseModel):
    repo: str
    prs: int = 20
    post: bool = True


class GuardrailOutcome(BaseModel):
    status: str  # proposed | none | skipped_too_little_feedback
    proposed: list[str] = []
    issue_number: int | None = None


async def build_guardrails(settings, db, driver, gh, req: GuardrailRequest) -> GuardrailOutcome:
    pulls = await gh.merged_pulls(req.repo, req.prs)
    numbers = {p["number"] for p in pulls}
    feedback = []
    for p in pulls:
        for c in await gh.review_comments(req.repo, p["number"]) + await gh.issue_comments(req.repo, p["number"]):
            if is_noise(c) or (c.get("user") or {}).get("login") == (p.get("user") or {}).get("login"):
                continue  # skip bots, KEEP, chatter, and authors talking about their own PR
            where = f" on {c['path']}" if c.get("path") else ""
            feedback.append((p["number"], f"PR {p['number']} @{c['user']['login']}{where}: {c['body'].strip()[:500]}"))
    if len({n for n, _ in feedback}) < MIN_PRS:
        return GuardrailOutcome(status="skipped_too_little_feedback")

    existing = [x async for x in db.patterns.find({})]
    existing_rules = {x["rule"].strip().lower() for x in existing}
    prompt = ("Existing rules:\n" + ("\n".join(f"- {x['rule']}" for x in existing) or "(none)") +
              "\n\nReview comments:\n" + "\n".join(t for _, t in feedback)[:20000])
    r = await ask_claude(settings, db, "guardrail", SYSTEM, prompt, schema=SCHEMA)

    proposed = []
    for raw in r.data.get("rules", []):
        prs = {n for n in raw.get("pr_numbers", []) if n in numbers}
        language = (raw.get("language") or "").lower()
        if len(prs) < MIN_PRS or language not in set(LANGUAGES.values()) \
                or raw.get("rule", "").strip().lower() in existing_rules:
            continue  # the 3-PR rule and duplicate check are enforced here, not trusted to the model
        p = await save_pattern(db, driver, PatternIn(
            rule=raw["rule"], anti_pattern=raw["anti_pattern"], language=language, reason=raw["reason"],
            bad_example=raw.get("bad_example"), good_example=raw.get("good_example"),
            established_by=[x.lstrip("@") for x in raw.get("people", [])],
            source_url=f"https://github.com/{req.repo}/pull/{min(prs)}"), status="proposed")
        proposed.append((p, sorted(prs)))

    if not proposed:
        return GuardrailOutcome(status="none")
    body = ["## KEEP proposes coding rules", "",
            "Reviewers keep making these points. Approve a rule and PR Review starts checking it.", ""]
    for p, prs in proposed:
        body += [f"### {p.id} · {p.rule} ({p.language})", f"Flagged in PRs {', '.join(map(str, prs))}. {p.reason}"]
        if p.bad_example:
            body += ["", f"Bad: `{p.bad_example}`  ", f"Good: `{p.good_example}`"]
        body.append("")
    body += ["Reply `keep: approve KEEP-P-00x` to turn a rule on.", "", "— KEEP"]
    number = None
    if req.post:
        issue = await gh.create_issue(req.repo, "KEEP: proposed coding rules", "\n".join(body), labels=["keep-guardrail"])
        number = issue["number"]
        await set_state(db, f"guardrail:{req.repo}", last_issue=number)
    return GuardrailOutcome(status="proposed", proposed=[p.id for p, _ in proposed], issue_number=number)
