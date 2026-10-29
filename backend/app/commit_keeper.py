"""Commit Keeper: a ledger entry for every push, summarised by a local Ollama model (no Claude cost).

Code decides domains and breaking risk from the file list; the model only writes the summary.
Ledger lives in MongoDB (full record) and Neo4j (Commit node linked to files and author).
"""

import logging
import re
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

log = logging.getLogger("keep.commits")
ZERO = "0" * 40

DOMAINS = [
    ("tests", re.compile(r"(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|\.spec\.|(^|/)test_")),
    ("docs", re.compile(r"\.(md|rst|txt)$|(^|/)docs/")),
    ("security", re.compile(r"(?i)auth|security|crypto|secret|permission|password|token")),
    ("db", re.compile(r"(?i)(^|/)(migrations?|alembic|db|models?|schema)/|\.sql$")),
    ("schema", re.compile(r"(?i)schema|\.proto$|openapi|graphql")),
    ("router", re.compile(r"(?i)route|urls\.py|router")),
    ("api", re.compile(r"(?i)(^|/)api/|endpoint|handler|controller|views?\.py")),
    ("ui", re.compile(r"\.(tsx|jsx|vue|svelte|css|scss|html)$|(^|/)(components|pages|ui)/")),
    ("config", re.compile(r"\.(ya?ml|toml|ini|cfg|env|json)$|(^|/)config/")),
    ("infra", re.compile(r"(?i)dockerfile|docker-compose|\.github/|terraform|k8s|helm|\.tf$")),
]


def domains_for(paths: list[str]) -> list[str]:
    found = []
    for path in paths:
        hit = next((name for name, rx in DOMAINS if rx.search(path)), "other")
        if hit not in found:
            found.append(hit)
    return found


def breaking_risk(files: list[dict], domains: list[str]) -> str:
    lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    removed = any(f.get("status") == "removed" for f in files)
    if removed or "db" in domains or "schema" in domains or lines > 500:
        return "HIGH"
    if {"api", "router", "config", "security", "infra"} & set(domains) or lines > 150:
        return "MEDIUM"
    return "LOW"


class CommitRequest(BaseModel):
    repo: str
    ref: str
    before: str
    after: str


class LedgerEntry(BaseModel):
    id: str  # "KEEP Commit a1b2c3d" style is only for display; id is the short sha
    sha: str
    repo: str
    branch: str
    authors: list[str]
    date: datetime
    messages: list[str]
    added: list[str]
    modified: list[str]
    removed: list[str]
    domains: list[str]
    breaking_risk: str
    summary: str
    summary_by: str  # ollama model name, or "fallback"
    needs_sync: bool = False


SUMMARY_PROMPT = """Summarise this code push for a team's change log in 3-5 plain sentences.
Say what changed and why it matters. No bullet points, no preamble.

Commit messages:
{messages}

Files changed: {files}

Changed lines (excerpt):
{diff}"""


async def summarise(ollama_url: str, model: str, messages: list[str], files: list[dict],
                    client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    diff = "\n".join(f"--- {f['filename']}\n{(f.get('patch') or '')[:1500]}" for f in files[:10])[:6000]
    prompt = SUMMARY_PROMPT.format(messages="\n".join(messages)[:2000],
                                   files=", ".join(f["filename"] for f in files[:30]), diff=diff)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=120)
    try:
        r = await client.post(ollama_url.rstrip("/") + "/api/generate",
                              json={"model": model, "prompt": prompt, "stream": False,
                                    "options": {"temperature": 0.2}})
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        if text:
            return text, model
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Ollama summary failed, using commit messages: %s", e)
    finally:
        if owns:
            await client.aclose()
    return " ".join(m.splitlines()[0] for m in messages)[:600] or "(no message)", "fallback"


COMMIT_NODE = """
MERGE (c:Commit {sha: $sha})
SET c.repo = $repo, c.branch = $branch, c.risk = $risk, c.date = datetime($date)
FOREACH (path IN $files | MERGE (f:File {path: path}) MERGE (c)-[:TOUCHES]->(f))
FOREACH (login IN $authors | MERGE (p:Person {login: login}) MERGE (p)-[:AUTHORED]->(c))
"""


async def keep_commit(settings, db, driver, gh, req: CommitRequest, client: httpx.AsyncClient | None = None
                      ) -> LedgerEntry | None:
    if req.after == ZERO:
        return None  # branch deleted: nothing to record
    if await db.commit_ledger.find_one({"_id": req.after[:7]}):
        return None  # already recorded (GitHub redelivery)
    if req.before == ZERO:
        data = await gh.commit(req.repo, req.after)       # new branch: just the head commit
        commits, files = [data], data.get("files", [])
    else:
        data = await gh.compare(req.repo, req.before, req.after)
        commits, files = data.get("commits", []), data.get("files", [])

    messages = [c.get("commit", {}).get("message", "") for c in commits]
    authors = sorted({(c.get("author") or {}).get("login") or c.get("commit", {}).get("author", {}).get("name", "unknown")
                      for c in commits})
    paths = [f["filename"] for f in files]
    domains = domains_for(paths)
    summary, by = await summarise(settings.ollama_url, settings.summary_model, messages, files, client)
    date_raw = (commits[-1].get("commit", {}).get("author", {}).get("date") if commits else None)
    date = datetime.fromisoformat(date_raw.replace("Z", "+00:00")) if date_raw else datetime.now(timezone.utc)

    entry = LedgerEntry(
        id=req.after[:7], sha=req.after, repo=req.repo, branch=req.ref.removeprefix("refs/heads/"),
        authors=authors, date=date, messages=messages,
        added=[f["filename"] for f in files if f.get("status") == "added"],
        modified=[f["filename"] for f in files if f.get("status") in ("modified", "renamed", "changed")],
        removed=[f["filename"] for f in files if f.get("status") == "removed"],
        domains=domains, breaking_risk=breaking_risk(files, domains), summary=summary, summary_by=by,
    )
    doc = entry.model_dump(exclude={"id"})
    doc["_id"] = entry.id
    await db.commit_ledger.insert_one(doc)
    try:
        await driver.execute_query(COMMIT_NODE, sha=entry.sha, repo=entry.repo, branch=entry.branch,
                                   risk=entry.breaking_risk, date=entry.date.isoformat(),
                                   files=paths, authors=authors)
    except Exception as e:
        log.warning("Graph write failed for commit %s: %s", entry.id, e)
        await db.commit_ledger.update_one({"_id": entry.id}, {"$set": {"needs_sync": True}})
        entry.needs_sync = True
    return entry
