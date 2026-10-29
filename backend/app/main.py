import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.claude import BudgetExceeded, Budget, ClaudeError, ClaudeResult, ask_claude
from app.config import Settings
from app.embeddings import Embedder
from app.github_api import GitHub, GitHubError
from app.github_webhook import forward_to_n8n, is_valid_signature
from app.pr_review import ReviewOutcome, review_pr
from app.decision_extractor import ExtractOutcome, ExtractRequest, extract
from app.reply_handler import ReplyOutcome, ReplyRequest, handle_reply
from app.ids import Kind, parse_id
from app.models import Decision, DecisionIn
from app.search import SearchHit, SearchRequest, search_decisions
from app.sync import RepairResult, SyncReport, repair, sync_check
from app.store import LinkError, ensure_schema, get_decision, link_decisions, save_decision

log = logging.getLogger("keep")
DB_NAME = "keep"

TIMEOUT_SECONDS = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()  # raises if any setting is missing
    app.state.settings = settings
    app.state.mongo = AsyncMongoClient(
        settings.mongo_uri, serverSelectionTimeoutMS=TIMEOUT_SECONDS * 1000
    )
    app.state.neo4j = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=TIMEOUT_SECONDS,
    )
    app.state.db = app.state.mongo[DB_NAME]
    app.state.embedder = Embedder(settings.ollama_url, settings.embed_model, settings.embed_dim)
    try:
        await ensure_schema(app.state.neo4j, settings.embed_dim)
    except Exception as e:  # start anyway; /health shows neo4j down
        log.warning("Could not create Neo4j schema at startup: %s", e)
    yield
    await app.state.embedder.close()
    await app.state.mongo.close()
    await app.state.neo4j.close()


app = FastAPI(title="KEEP", lifespan=lifespan)


async def check_mongodb(client: AsyncMongoClient) -> bool:
    await client.admin.command("ping")
    return True


async def check_neo4j(driver) -> bool:
    await driver.verify_connectivity()
    return True


async def check_ollama(url: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
    return True


async def _status(check, *args) -> str:
    try:
        return "ok" if await check(*args) else "down"
    except Exception:
        return "down"


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    state = request.app.state
    return {
        "mongodb": await _status(check_mongodb, state.mongo),
        "neo4j": await _status(check_neo4j, state.neo4j),
        "ollama": await _status(check_ollama, state.settings.ollama_url),
    }


@app.post("/decisions", status_code=201)
async def create_decision(data: DecisionIn, request: Request) -> Decision:
    state = request.app.state
    return await save_decision(state.db, state.neo4j, state.embedder, data)


@app.get("/decisions/{decision_id}")
async def read_decision(decision_id: str, request: Request) -> Decision:
    try:
        kind, _ = parse_id(decision_id)
    except ValueError:
        raise HTTPException(400, f"Not a KEEP ID: {decision_id}")
    if kind != Kind.DECISION:
        raise HTTPException(400, f"Not a decision ID: {decision_id}")
    decision = await get_decision(request.app.state.db, decision_id)
    if decision is None:
        raise HTTPException(404, f"{decision_id} not found")
    return decision


@app.post("/search")
async def search(req: SearchRequest, request: Request) -> list[SearchHit]:
    state = request.app.state
    return await search_decisions(state.db, state.neo4j, state.embedder, req)


class LinkRequest(BaseModel):
    depends_on: str


@app.post("/decisions/{decision_id}/links")
async def add_link(decision_id: str, body: LinkRequest, request: Request) -> Decision:
    state = request.app.state
    try:
        return await link_decisions(state.db, state.neo4j, decision_id, body.depends_on)
    except LinkError as e:
        raise HTTPException(409 if str(e).startswith("Loop") else 400, str(e))


@app.get("/sync/check")
async def check_sync(request: Request) -> SyncReport:
    return await sync_check(request.app.state.db, request.app.state.neo4j)


@app.post("/sync/repair")
async def repair_sync(request: Request) -> RepairResult:
    state = request.app.state
    return await repair(state.db, state.neo4j, state.embedder)


@app.post("/github/webhook", status_code=202)
async def github_webhook(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    body = await request.body()  # raw bytes: the signature is over these exactly
    if not is_valid_signature(settings.github_webhook_secret, body,
                              request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Bad or missing signature")
    try:
        status = await forward_to_n8n(settings.n8n_router_url, body, dict(request.headers))
    except httpx.HTTPError as e:
        # 502 makes the delivery show as failed in GitHub, so it can be redelivered.
        raise HTTPException(502, f"n8n unreachable: {e}")
    if status >= 400:
        raise HTTPException(502, f"n8n answered {status}")
    return {"status": "accepted"}


@app.get("/budget")
async def budget(request: Request) -> dict:
    s = request.app.state.settings
    return await Budget(request.app.state.db, s.daily_call_cap, s.daily_token_cap).usage()


class PingRequest(BaseModel):
    text: str = "Say OK"
    structured: bool = False


PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
               "required": ["ok", "word"]}


@app.post("/claude/ping")
async def claude_ping(body: PingRequest, request: Request) -> ClaudeResult:
    """Smoke test for the Claude call path (cheap Haiku call, counted in the budget)."""
    state = request.app.state
    try:
        return await ask_claude(
            state.settings, state.db, "ping",
            system="You are a health check. Answer in as few words as possible.",
            prompt=body.text if not body.structured else "Return ok=true and one random word.",
            schema=PING_SCHEMA if body.structured else None,
        )
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except ClaudeError as e:
        raise HTTPException(502, str(e))


class ReviewRequest(BaseModel):
    repo: str
    number: int
    post: bool = True  # False = dry run: build the comment but do not post it


@app.post("/agents/pr-review")
async def pr_review(body: ReviewRequest, request: Request) -> ReviewOutcome:
    state = request.app.state
    try:
        gh = GitHub(state.settings.github_token)
    except GitHubError as e:
        raise HTTPException(500, str(e))
    try:
        return await review_pr(state.settings, state.db, state.neo4j, state.embedder, gh,
                               body.repo, body.number, post=body.post)
    except (GitHubError, ClaudeError) as e:
        raise HTTPException(502, str(e))
    finally:
        await gh.close()


async def _with_github(request: Request, fn):
    """Run an agent with a GitHub client; map agent errors to clear HTTP errors."""
    try:
        gh = GitHub(request.app.state.settings.github_token)
    except GitHubError as e:
        raise HTTPException(500, str(e))
    try:
        return await fn(gh)
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except (GitHubError, ClaudeError) as e:
        raise HTTPException(502, str(e))
    finally:
        await gh.close()


@app.post("/agents/decision-extractor")
async def decision_extractor(body: ExtractRequest, request: Request) -> ExtractOutcome:
    s = request.app.state
    return await _with_github(request, lambda gh: extract(
        s.settings, s.db, s.neo4j, s.embedder, gh, body.repo, body.number, post=body.post))


@app.post("/agents/reply-handler")
async def reply_handler(body: ReplyRequest, request: Request) -> ReplyOutcome:
    s = request.app.state
    return await _with_github(request, lambda gh: handle_reply(s.settings, s.db, s.neo4j, s.embedder, gh, body))
