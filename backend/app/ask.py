"""Ask: answer a question from team memory, citing decision IDs. `keep: ask <question>`."""

from app.claude import ask_claude
from app.pr_review import decisions_text
from app.search import SearchRequest, search_decisions

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "cited": {"type": "array", "items": {"type": "string"}}},
    "required": ["answer", "cited"],
}

SYSTEM = """You answer questions about a codebase using ONLY the team's recorded decisions below.
Direct, specific, a bit dry. 2-5 sentences. Cite decision IDs inline like (KEEP-D-001).
Superseded decisions are history: say what replaced them. If the decisions don't answer the
question, say so plainly and don't guess. "cited" lists every ID you used."""


async def answer(settings, db, driver, embedder, question: str) -> tuple[str, list]:
    hits = await search_decisions(db, driver, embedder, SearchRequest(
        text=question[:2000], k=8, min_score=0.0, include_inactive=True))  # loose wording: Claude judges
    if not hits:
        return "Nothing in memory about that yet.", []
    decisions = {h.decision.id: h.decision for h in hits}
    listing = "\n\n".join(f"{decisions_text([d])}\nStatus: {d.status.value}" for d in decisions.values())
    r = await ask_claude(settings, db, "ask", SYSTEM, f"Question: {question}\n\n## Decisions\n{listing}", schema=SCHEMA)
    cited = [decisions[i] for i in r.data.get("cited", []) if i in decisions]
    return r.data.get("answer", "").strip(), cited


def ask_comment(question: str, text: str, cited: list) -> str:
    lines = [f"**KEEP:** {text}"]
    if cited:
        lines += ["", "Sources:"]
        lines += [f"- **{d.id}** · {d.title}" + (f" ({d.status.value})" if d.status.value != "active" else "")
                  for d in cited]
    lines += ["", "— KEEP"]
    return "\n".join(lines)
