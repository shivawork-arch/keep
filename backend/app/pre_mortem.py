"""Pre-Mortem: when an issue is opened, warn about past decisions before anyone writes code.

Code finds related decisions (by meaning). No related decisions -> no Claude call, no comment.
Claude (Sonnet) turns them into specific risks and a short checklist.
"""

from pydantic import BaseModel

from app.claude import ask_claude
from app.pr_review import SOURCE_LABEL, decisions_text
from app.search import SearchRequest, search_decisions

SCHEMA = {
    "type": "object",
    "properties": {
        "risks": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "decision_id": {"type": "string"},
                "risk": {"type": "string"},
                "advice": {"type": "string"}},
                "required": ["decision_id", "risk", "advice"]},
        },
        "checklist": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["risks", "checklist", "summary"],
}

SYSTEM = """You run a pre-mortem on a new issue before anyone writes code.
You are an experienced engineer who has seen these exact things break. Direct, a bit dry, specific.

Given the issue and the team's recorded decisions, imagine the work shipped and broke something
the team already decided about. For each decision that the work is likely to run into:
- risk: how this issue's work could go against it, in one or two sentences.
- advice: what to do instead, concretely.
Only use decisions that genuinely apply; skip the rest. Only use decision IDs from the list.
checklist: up to 4 short items to confirm before the PR is opened. summary: one dry sentence.
If no decision applies, return empty risks and checklist."""


class PreMortemRequest(BaseModel):
    repo: str
    number: int
    post: bool = True


class PreMortemOutcome(BaseModel):
    status: str  # warned | nothing_relevant | skipped_no_memory | skipped_pull_request
    risks: list[dict] = []
    checklist: list[str] = []
    comment: str | None = None


def build_comment(risks: list[dict], decisions: dict, checklist: list[str], summary: str) -> str:
    lines = ["## KEEP pre-mortem", "", summary, "", "Past decisions this work will run into:", ""]
    for r in risks:
        d = decisions[r["decision_id"]]
        lines += [f"### {d.id} · {d.title}", r["risk"], "", f"**Instead:** {r['advice']}  ",
                  f"**Rule on record:** {d.future_implication} "
                  f"([{SOURCE_LABEL[d.source.type.value]} {d.source.ref}]({d.source.url}))", ""]
    if checklist:
        lines += ["**Before opening the PR:**", *[f"- [ ] {c}" for c in checklist[:4]], ""]
    lines.append("— KEEP")
    return "\n".join(lines)


async def pre_mortem(settings, db, driver, embedder, gh, repo: str, number: int, post: bool = True) -> PreMortemOutcome:
    issue = await gh.issue(repo, number)
    if "pull_request" in issue:  # GitHub returns PRs from the issues API too
        return PreMortemOutcome(status="skipped_pull_request")
    text = f"{issue.get('title')}\n{issue.get('body') or ''}"
    hits = await search_decisions(db, driver, embedder, SearchRequest(
        text=text[:4000], k=settings.review_k, min_score=settings.review_min_score))
    if not hits:
        return PreMortemOutcome(status="skipped_no_memory")

    decisions = {h.decision.id: h.decision for h in hits}
    prompt = (f"Issue {number}: {issue.get('title')}\n{issue.get('body') or '(no description)'}\n\n"
              f"## Decisions on record\n{decisions_text(list(decisions.values()))}")
    result = await ask_claude(settings, db, "pre_mortem", SYSTEM, prompt, schema=SCHEMA)
    risks = [r for r in result.data.get("risks", []) if r.get("decision_id") in decisions
             and r.get("risk") and r.get("advice")]
    if not risks:
        return PreMortemOutcome(status="nothing_relevant")
    checklist = [c for c in result.data.get("checklist", []) if isinstance(c, str) and c.strip()]
    comment = build_comment(risks, decisions, checklist, result.data.get("summary", ""))
    if post:
        await gh.comment(repo, number, comment)
    return PreMortemOutcome(status="warned", risks=risks, checklist=checklist, comment=comment)
