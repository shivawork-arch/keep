from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.config import Settings

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
    yield
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
