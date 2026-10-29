"""Incident Linker: an issue labelled `incident` becomes KEEP-I-001 and gets linked to the
decisions it relates to, with a flag when a recent override or superseded rule may be the cause.

Code: label check, incident ID, graph links, "recent override" rule. Claude (Haiku): which
candidate decisions really relate, and how.
"""

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.claude import ask_claude
from app.ids import Kind, next_id
from app.pr_review import decisions_text
from app.search import SearchRequest, search_decisions

log = logging.getLogger("keep.incidents")
INCIDENT_LABELS = {"incident", "outage", "postmortem"}
RECENT_DAYS = 30

SCHEMA = {
    "type": "object",
    "properties": {
        "links": {"type": "array", "items": {"type": "object", "properties": {
            "decision_id": {"type": "string"}, "relation": {"type": "string"}},
            "required": ["decision_id", "relation"]}},
        "summary": {"type": "string"},
    },
    "required": ["links", "summary"],
}

SYSTEM = """An incident was reported. Decide which of the listed team decisions it genuinely relates to.
For each, one sentence in "relation": how the decision bears on the incident (e.g. the incident is what
the decision was meant to prevent, or a superseded/overridden rule allowed it).
Only use IDs from the list. Skip weak matches. summary: one dry sentence."""


class IncidentRequest(BaseModel):
    repo: str
    number: int
    label: str | None = None
    post: bool = True


class IncidentOutcome(BaseModel):
    status: str  # linked | no_links | skipped_not_incident | skipped_already_done
    incident_id: str | None = None
    links: list[dict] = []
    suspects: list[str] = []
    comment: str | None = None


INCIDENT_NODE = """
MERGE (i:Incident {id: $id})
SET i.title = $title, i.url = $url, i.created_at = datetime($created_at)
FOREACH (d IN $decisions | MERGE (x:Decision {id: d}) MERGE (i)-[:RELATES_TO]->(x))
"""


def as_utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def link_incident(settings, db, driver, embedder, gh, req: IncidentRequest,
                        now: datetime | None = None) -> IncidentOutcome:
    now = now or datetime.now(timezone.utc)
    issue = await gh.issue(req.repo, req.number)
    labels = {(l.get("name") or "").lower() for l in issue.get("labels", [])}
    if not (labels & INCIDENT_LABELS):
        return IncidentOutcome(status="skipped_not_incident")
    key = f"{req.repo}#{req.number}"
    if await db.incidents.find_one({"source": key}):
        return IncidentOutcome(status="skipped_already_done")

    text = f"{issue.get('title')}\n{issue.get('body') or ''}"
    hits = await search_decisions(db, driver, embedder, SearchRequest(
        text=text[:4000], k=6, min_score=settings.review_min_score, include_inactive=True))
    decisions = {h.decision.id: h.decision for h in hits}
    links, summary = [], ""
    if decisions:
        listing = "\n\n".join(f"{decisions_text([d])}\nStatus: {d.status.value}" for d in decisions.values())
        r = await ask_claude(settings, db, "incident_link", SYSTEM,
                             f"Incident: {text}\n\n## Decisions\n{listing}", schema=SCHEMA, urgent=True)
        links = [l for l in r.data.get("links", []) if l.get("decision_id") in decisions and l.get("relation")]
        summary = r.data.get("summary", "")

    # Fixed rule: a linked decision changed (override/supersede) in the last 30 days is a suspect.
    suspects = []
    for l in links:
        d = decisions[l["decision_id"]]
        changed = [h for h in d.history if h.change.startswith(("superseded by", "overrides"))]
        if changed and as_utc(changed[-1].at) >= now - timedelta(days=RECENT_DAYS):
            suspects.append(d.id)

    incident_id = await next_id(db, Kind.INCIDENT)
    await db.incidents.insert_one({"_id": incident_id, "source": key, "title": issue.get("title"),
                                   "url": issue.get("html_url"), "created_at": now,
                                   "decisions": [l["decision_id"] for l in links], "suspects": suspects,
                                   "summary": summary, "needs_sync": False})
    try:
        await driver.execute_query(INCIDENT_NODE, id=incident_id, title=issue.get("title"), url=issue.get("html_url"),
                                   created_at=now.isoformat(), decisions=[l["decision_id"] for l in links])
    except Exception as e:
        log.warning("Graph write failed for %s: %s", incident_id, e)
        await db.incidents.update_one({"_id": incident_id}, {"$set": {"needs_sync": True}})

    if links:
        lines = [f"## KEEP: recorded as {incident_id}", "", summary, "", "Related decisions:", ""]
        for l in links:
            d = decisions[l["decision_id"]]
            flag = " **changed recently**" if d.id in suspects else ""
            lines.append(f"- **{d.id}** · {d.title} ({d.status.value}){flag}: {l['relation']}")
        if suspects:
            lines += ["", f"Check the recent change to {', '.join(suspects)} first."]
        lines += ["", "— KEEP"]
        status = "linked"
    else:
        lines = [f"**KEEP:** Recorded as {incident_id}. Nothing in memory relates to it yet.", "", "— KEEP"]
        status = "no_links"
    comment = "\n".join(lines)
    if req.post:
        await gh.comment(req.repo, req.number, comment)
    return IncidentOutcome(status=status, incident_id=incident_id, links=links, suspects=suspects, comment=comment)
