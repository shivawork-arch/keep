"""Decision Decay (scheduled): find decisions the code may have outgrown, and ask whether they still hold.

Code picks candidates: active decisions whose files changed a lot since the decision, or that are old.
Claude (Haiku) judges each one against recent change summaries. Code applies the result: an outdated
decision loses one confidence level and gets a history line. It is never superseded automatically.
"""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.claude import BudgetExceeded, ask_claude
from app.store import from_mongo

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["still_valid", "likely_outdated", "unclear"]},
                   "note": {"type": "string"}},
    "required": ["verdict", "note"],
}

SYSTEM = """A team recorded a decision. Since then, the files it governs changed several times.
From the change summaries, judge whether the decision still holds.
- still_valid: the changes are consistent with it.
- likely_outdated: the changes moved away from it, or the code it governs was replaced/removed.
- unclear: not enough signal.
note: one dry sentence pointing at the change that matters."""

LOWER = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}


class DecayRequest(BaseModel):
    limit: int = 10
    min_touches: int = 3
    max_age_days: int = 180
    recheck_days: int = 30


class DecayOutcome(BaseModel):
    checked: dict[str, str] = {}
    lowered: list[str] = []
    stopped_for_budget: bool = False


def as_utc(dt):
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def decay(settings, db, req: DecayRequest, now: datetime | None = None) -> DecayOutcome:
    now = now or datetime.now(timezone.utc)
    ledger = []
    async for c in db.commit_ledger.find({}):
        ledger.append(c)

    candidates = []
    async for doc in db.decisions.find({"status": "active"}):
        d = from_mongo(doc)
        last = (doc.get("decay") or {}).get("checked_at")
        if last and as_utc(last) > now - timedelta(days=req.recheck_days):
            continue
        created = as_utc(d.created_at)
        touching = [c for c in ledger if as_utc(c["date"]) > created
                    and set(d.files) & set(c.get("added", []) + c.get("modified", []) + c.get("removed", []))]
        old = created < now - timedelta(days=req.max_age_days)
        if d.files and (len(touching) >= req.min_touches or old):
            candidates.append((len(touching), d, touching))
    candidates.sort(key=lambda x: -x[0])

    out = DecayOutcome()
    for _, d, touching in candidates[:req.limit]:
        changes = "\n".join(f"- {c['_id']} ({c['breaking_risk']}): {c['summary']}" for c in touching[-15:]) or "(none recorded)"
        prompt = (f"Decision {d.id}: {d.title}\nDecision: {d.decision}\nRule: {d.future_implication}\n"
                  f"Files: {', '.join(d.files)}\nRecorded: {as_utc(d.created_at).date()}\n\nChanges since:\n{changes}")
        try:
            r = await ask_claude(settings, db, "decay_check", SYSTEM, prompt, schema=SCHEMA)
        except BudgetExceeded:
            out.stopped_for_budget = True
            break
        verdict = r.data.get("verdict", "unclear")
        update = {"$set": {"decay": {"verdict": verdict, "note": r.data.get("note", ""), "checked_at": now}}}
        if verdict == "likely_outdated":
            update["$set"]["confidence"] = LOWER[d.confidence.value]
            update["$push"] = {"history": {"at": now, "change": f"decay check: likely outdated ({r.data.get('note', '')})"}}
            out.lowered.append(d.id)
        await db.decisions.update_one({"_id": d.id}, update)
        out.checked[d.id] = verdict
    return out
