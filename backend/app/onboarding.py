"""Onboarding brief: what a new developer should know, built from memory in code (no Claude call).

Order (from LORE): security decisions, architecture by file, pattern rules, key people,
recent superseded decisions, last changes.
"""

from collections import Counter, defaultdict

from app.store import from_mongo


def first_sentence(text: str, cap: int = 200) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    end = line.find(". ")
    line = line[:end + 1] if end != -1 else line
    return line if len(line) <= cap else line[:cap].rsplit(" ", 1)[0] + "..."


async def brief(db, limit_changes: int = 10) -> str:
    decisions = []
    async for doc in db.decisions.find({}):
        decisions.append(from_mongo(doc))
    active = [d for d in decisions if d.status.value == "active"]
    if not active:
        return "**KEEP onboarding:** Memory is empty so far. Decisions appear as PRs merge.\n\n— KEEP"

    lines = ["## KEEP onboarding brief", "",
             f"{len(active)} active decisions on record. Read these before your first PR.", ""]

    security = [d for d in active if d.security_relevant]
    if security:
        lines += ["### Security rules", *[f"- **{d.id}** · {d.future_implication}" for d in security], ""]

    by_area = defaultdict(list)
    for d in active:
        for f in d.files or ["(general)"]:
            top = f.split("/")[0] if "/" in f else f
            by_area[top].append(d)
    lines.append("### By area")
    for area in sorted(by_area):
        seen = []
        for d in by_area[area]:
            if d.id not in seen:
                seen.append(d.id)
                lines.append(f"- `{area}` **{d.id}** · {d.title}: {d.future_implication}")
    lines.append("")

    patterns = []
    async for doc in db.patterns.find({"status": "active"}):
        patterns.append(doc)
    if patterns:
        lines += ["### Coding rules", *[f"- **{p['_id']}** ({p['language']}) {p['rule']}" for p in patterns], ""]

    people = Counter(p for d in active for p in d.decided_by)
    if people:
        lines += ["### Who to ask", *[f"- @{p}: {n} decision{'s' if n > 1 else ''}" for p, n in people.most_common(5)], ""]

    superseded = [d for d in decisions if d.status.value == "superseded"]
    if superseded:
        lines += ["### Rules that changed", *[f"- **{d.id}** · {d.title} (superseded)" for d in superseded[-5:]], ""]

    changes = []
    async for doc in db.commit_ledger.find({}):
        changes.append(doc)
    changes.sort(key=lambda c: c["date"], reverse=True)
    if changes:
        lines.append("### Recent changes")
        lines += [f"- `{c['_id']}` ({c['breaking_risk']}) {first_sentence(c['summary'])}"
                  for c in changes[:limit_changes]]
        lines.append("")

    lines.append("— KEEP")
    return "\n".join(lines)
