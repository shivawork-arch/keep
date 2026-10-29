"""Migrate: build memory from a repo's past merged PRs by running the Decision Extractor over them.

Run once when KEEP is added to an existing repo. Already-extracted PRs are skipped, so it is
safe to run again. Non-urgent: it stops cleanly when the daily budget is reached.
"""

from pydantic import BaseModel

from app.claude import BudgetExceeded
from app.decision_extractor import extract


class MigrateRequest(BaseModel):
    repo: str
    limit: int = 20
    post: bool = False  # by default, don't comment on old PRs


class MigrateOutcome(BaseModel):
    scanned: int
    recorded: dict[str, list[str]] = {}
    stopped_for_budget: bool = False


async def migrate(settings, db, driver, embedder, gh, req: MigrateRequest) -> MigrateOutcome:
    pulls = await gh.merged_pulls(req.repo, req.limit)
    out = MigrateOutcome(scanned=0)
    for p in reversed(pulls):  # oldest first, so later PRs can be checked against earlier decisions
        try:
            # post=True marks the PR as done so reruns skip it; comment_on_pr controls the GitHub comment.
            r = await extract(settings, db, driver, embedder, gh, req.repo, p["number"],
                              post=True, comment_on_pr=req.post)
        except BudgetExceeded:
            out.stopped_for_budget = True
            break
        out.scanned += 1
        if r.recorded:
            out.recorded[str(p["number"])] = r.recorded
    return out
