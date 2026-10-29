"""Reply Handler: acts on `keep:` replies to a KEEP review.

Commands are parsed in code (no AI routing):
  keep: intentional — <reason>   override: new decision recorded, old one superseded, links moved
  keep: accidental               the decision stands; next push is checked again
  keep: discuss                  ping the people who made the decision
A command can name decisions ("keep: intentional KEEP-D-001 — reason"); otherwise it applies to
the decisions flagged in this PR's latest KEEP review.
"""

import re

from pydantic import BaseModel

from app.claude import ClaudeError, ask_claude
from app.models import Confidence, DecisionIn, Origin
from app.store import get_decision, save_decision, supersede

COMMAND = re.compile(r"^\s*keep:\s*(intentional|accidental|discuss)\b(.*)$", re.IGNORECASE | re.DOTALL)
IDS = re.compile(r"KEEP-D-\d{3,}")
REASON_SPLIT = re.compile(r"^[\s\-–—:]+")


class ReplyRequest(BaseModel):
    repo: str
    number: int
    comment_id: int | None = None
    author: str
    text: str
    post: bool = True


class ReplyOutcome(BaseModel):
    action: str  # intentional | accidental | discuss | unknown | no_target | needs_reason
    targets: list[str] = []
    created: list[str] = []
    comment: str | None = None


def parse(text: str) -> tuple[str | None, list[str], str]:
    """Returns (command, named decision IDs, reason)."""
    m = COMMAND.match(text.strip())
    if not m:
        return None, [], ""
    rest = m.group(2)
    ids = IDS.findall(rest)
    reason = REASON_SPLIT.sub("", IDS.sub("", rest)).strip()
    return m.group(1).lower(), ids, reason


OVERRIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "decision": {"type": "string"},
        "future_implication": {"type": "string"},
    },
    "required": ["title", "decision", "future_implication"],
}

OVERRIDE_SYSTEM = """A developer deliberately went against a recorded team decision and gave a reason.
Write the NEW decision that replaces the old one, as the team's rule from now on.
- title: under 8 words.
- decision: what is now allowed or chosen, one sentence.
- future_implication: the rule for future developers, including when the old rule still applies if the reason implies it.
Plain, specific, no filler."""


async def write_override(settings, db, old, reason: str) -> dict:
    prompt = (f"Old decision {old.id}: {old.title}\nDecision: {old.decision}\nRule: {old.future_implication}\n"
              f"Reason it was kept: {old.reason}\n\nDeveloper's reason for going against it: {reason}")
    try:
        r = await ask_claude(settings, db, "override_write", OVERRIDE_SYSTEM, prompt, schema=OVERRIDE_SCHEMA, urgent=True)
        return r.data
    except ClaudeError:
        # Fallback keeps the command working even if Claude is unavailable.
        return {"title": f"Exception to {old.id}", "decision": reason,
                "future_implication": f"{old.future_implication}, except: {reason}"}


async def handle_reply(settings, db, driver, embedder, gh, req: ReplyRequest) -> ReplyOutcome:
    command, named, reason = parse(req.text)
    if command is None:
        out = ReplyOutcome(action="unknown", comment=(
            "**KEEP:** I didn't catch that. Use `keep: intentional — <reason>`, `keep: accidental` "
            "or `keep: discuss`.\n\n— KEEP"))
    else:
        if named:
            targets = named
        else:
            review = await db.pr_reviews.find_one({"_id": f"{req.repo}#{req.number}"}) or {}
            targets = review.get("last_conflicts", [])
        decisions = [d for d in [await get_decision(db, t) for t in targets] if d is not None]

        if not decisions:
            out = ReplyOutcome(action="no_target", comment=(
                "**KEEP:** There's no flagged decision on this PR to apply that to. "
                "Name one, e.g. `keep: discuss KEEP-D-001`.\n\n— KEEP"))
        elif command == "accidental":
            ids = ", ".join(d.id for d in decisions)
            out = ReplyOutcome(action="accidental", targets=[d.id for d in decisions], comment=(
                f"**KEEP:** Noted, @{req.author}. {ids} stands. I'll check the next push.\n\n— KEEP"))
        elif command == "discuss":
            people = sorted({p for d in decisions for p in d.decided_by} - {req.author})
            ids = ", ".join(f"**{d.id}** · {d.title}" for d in decisions)
            if people:
                opener = f"{' '.join('@' + p for p in people)}, @{req.author} wants to revisit {ids}."
            else:
                opener = f"@{req.author}, you're the only one on record for {ids}, so there's nobody else to ping."
            out = ReplyOutcome(action="discuss", targets=[d.id for d in decisions], comment=(
                f"**KEEP:** {opener}\n\nOriginal reason: {decisions[0].reason}\n\n— KEEP"))
        elif not reason:
            out = ReplyOutcome(action="needs_reason", targets=[d.id for d in decisions], comment=(
                "**KEEP:** An override needs a reason: `keep: intentional — <why>`. "
                "The reason is what the next person reads.\n\n— KEEP"))
        else:  # intentional, with a reason
            pr = await gh.pull(req.repo, req.number)
            created = []
            for old in decisions:
                text = await write_override(settings, db, old, reason)
                new = await save_decision(db, driver, embedder, DecisionIn(
                    title=text["title"][:120], decision=text["decision"], rejected=old.decision,
                    reason=reason, future_implication=text["future_implication"],
                    source={"type": "comment", "url": pr["html_url"], "ref": str(req.comment_id or req.number)},
                    decided_by=[req.author], files=old.files, confidence=Confidence.HIGH,
                    origin=Origin.DISCUSSION, security_relevant=old.security_relevant,
                ))
                await supersede(db, driver, old.id, new.id)
                created.append((new.id, old.id, new.title))
            lines = [f"**KEEP:** Override recorded, @{req.author}.", ""]
            lines += [f"- **{n}** · {t} (replaces **{o}**, now superseded)" for n, o, t in created]
            lines += ["", f"Reason on record: {reason}", "", "— KEEP"]
            out = ReplyOutcome(action="intentional", targets=[d.id for d in decisions],
                               created=[n for n, _, _ in created], comment="\n".join(lines))

    if req.post and out.comment:
        await gh.comment(req.repo, req.number, out.comment)
    return out
