"""Follow-Up (scheduled): nudge PRs where KEEP flagged a conflict that nobody answered.

Rules in code: only open PRs, only unresolved conflicts, only once, only after `hours` have passed.
No Claude call: the nudge is a fixed message.
"""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.store import get_decision


class FollowUpRequest(BaseModel):
    hours: int = 48
    post: bool = True


class FollowUpOutcome(BaseModel):
    nudged: list[str] = []
    closed_skipped: list[str] = []


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # Mongo returns naive UTC


async def follow_up(db, gh, hours: int = 48, post: bool = True, now: datetime | None = None) -> FollowUpOutcome:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    out = FollowUpOutcome()
    async for doc in db.pr_reviews.find({"resolved": False, "followed_up": False}):
        if not doc.get("last_conflicts") or not doc.get("reviewed_at") or as_utc(doc["reviewed_at"]) > cutoff:
            continue
        repo, number = doc["_id"].rsplit("#", 1)
        pr = await gh.pull(repo, int(number))
        if pr.get("state") != "open":
            out.closed_skipped.append(doc["_id"])
            if post:
                await db.pr_reviews.update_one({"_id": doc["_id"]}, {"$set": {"followed_up": True}})
            continue
        titles = []
        for i in doc["last_conflicts"]:
            d = await get_decision(db, i)
            titles.append(f"**{i}** · {d.title}" if d else f"**{i}**")
        author = (pr.get("user") or {}).get("login")
        body = (f"**KEEP:** {'@' + author + ', t' if author else 'T'}his PR still conflicts with "
                f"{', '.join(titles)} and nobody has answered.\n\n"
                "Reply `keep: intentional — <reason>`, `keep: accidental`, or `keep: discuss`.\n\n— KEEP")
        if post:
            await gh.comment(repo, int(number), body)
            await db.pr_reviews.update_one({"_id": doc["_id"]}, {"$set": {"followed_up": True}})
        out.nudged.append(doc["_id"])
    return out
