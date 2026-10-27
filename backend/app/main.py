import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.config import Settings
from app.embeddings import Embedder
from app.ids import Kind, parse_id
from app.models import Decision, DecisionIn
from app.search import SearchHit, SearchRequest, search_decisions
from app.store import ensure_schema, get_decision, save_decision

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
