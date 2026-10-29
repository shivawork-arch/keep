"""Health Auditor (scheduled): one GitHub issue, kept up to date, showing the state of KEEP's memory.

Built entirely in code (no Claude call): totals, sync state, coverage gaps, weak spots, decay flags.
"""

from collections import Counter
from datetime import datetime, timezone

from pydantic import BaseModel

from app.claude import Budget
from app.state import get_state, set_state
from app.store import from_mongo
from app.sync import sync_check

TITLE = "KEEP health report"


class HealthRequest(BaseModel):
    repo: str
    post: bool = True


class HealthOutcome(BaseModel):
    issue_number: int | None = None
    report: str


async def build_report(settings, db, driver) -> str:
    decisions = [from_mongo(d) async for d in db.decisions.find({})]
    active = [d for d in decisions if d.status.value == "active"]
    patterns = [p async for p in db.patterns.find({})]
    incidents = [i async for i in db.incidents.find({})]
    ledger = [c async for c in db.commit_ledger.find({})]
    now = datetime.now(timezone.utc)

    lines = [f"## {TITLE}", f"_Updated {now:%Y-%m-%d %H:%M} UTC_", "",
             "| | Count |", "|---|---|",
             f"| Active decisions | {len(active)} |",
             f"| Superseded / overridden | {len(decisions) - len(active)} |",
             f"| Pattern rules (active / proposed) | {sum(p.get('status') == 'active' for p in patterns)} / "
             f"{sum(p.get('status') == 'proposed' for p in patterns)} |",
             f"| Incidents | {len(incidents)} |",
             f"| Commits in ledger | {len(ledger)} |", ""]

    try:
        sync = await sync_check(db, driver)
        bad = len(sync.only_in_mongo) + len(sync.only_in_neo4j) + len(sync.needs_sync)
        lines += ["### Sync", "MongoDB and Neo4j are in step." if bad == 0 else
                  f"{bad} record(s) out of step: run `POST /sync/repair`.", ""]
    except Exception:
        lines += ["### Sync", "Neo4j unreachable during this check.", ""]

    governed = {f for d in active for f in d.files}
    touched = Counter(f for c in ledger for f in c.get("added", []) + c.get("modified", []))
    gaps = [(f, n) for f, n in touched.most_common() if f not in governed and not f.lower().endswith((".md", ".txt"))][:5]
    if gaps:
        lines += ["### Busy files with no decision on record",
                  *[f"- `{f}`: changed in {n} commit(s)" for f, n in gaps], ""]

    weak = [d for d in active if d.confidence.value == "LOW"]
    if weak:
        lines += ["### Low confidence", *[f"- **{d.id}** · {d.title}" for d in weak], ""]

    flagged = []
    async for doc in db.decisions.find({"status": "active"}):
        if (doc.get("decay") or {}).get("verdict") == "likely_outdated":
            flagged.append(doc)
    if flagged:
        lines += ["### Possibly outdated (decay check)",
                  *[f"- **{d['_id']}** · {d['title']}: {d['decay']['note']}" for d in flagged], ""]

    no_files = [d for d in active if not d.files]
    if no_files:
        lines += ["### Decisions not tied to any file", *[f"- **{d.id}** · {d.title}" for d in no_files], ""]

    overridden = Counter(f.split("/")[0] for d in decisions if d.status.value != "active" for f in d.files)
    if overridden:
        lines += ["### Most overridden areas", *[f"- `{a}`: {n}" for a, n in overridden.most_common(3)], ""]

    linked = Counter(x for i in incidents for x in i.get("decisions", []))
    if linked:
        lines += ["### Decisions linked to incidents", *[f"- **{x}**: {n} incident(s)" for x, n in linked.most_common(5)], ""]

    u = await Budget(db, settings.daily_call_cap, settings.daily_token_cap).usage()
    lines += ["### Claude usage today", f"{u['calls']} / {u['call_cap']} calls, {u['tokens']:,} tokens.", "", "— KEEP"]
    return "\n".join(lines)


async def audit(settings, db, driver, gh, req: HealthRequest) -> HealthOutcome:
    report = await build_report(settings, db, driver)
    if not req.post:
        return HealthOutcome(report=report)
    key = f"health:{req.repo}"
    number = (await get_state(db, key)).get("issue")
    if number:
        await gh.update_issue(req.repo, number, report)
    else:
        issue = await gh.create_issue(req.repo, TITLE, report, labels=["keep-health"])
        number = issue["number"]
        await set_state(db, key, issue=number)
    return HealthOutcome(issue_number=number, report=report)
