"""DEV ONLY: wipe all KEEP memory (MongoDB + Neo4j), then optionally seed decisions.

Seeding goes through save_decision, so seeded decisions get IDs, vectors and graph links
exactly like real ones.

Usage (from backend/, venv on):
    python reset_memory.py                     # wipe only
    python reset_memory.py ../scripts/seed.json  # wipe, then seed
"""

import asyncio
import json
import sys

from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.config import Settings
from app.embeddings import Embedder
from app.models import DecisionIn
from app.store import ensure_schema, save_decision


async def main(seed_file: str | None):
    s = Settings()
    mongo = AsyncMongoClient(s.mongo_uri)
    db = mongo["keep"]
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    embedder = Embedder(s.ollama_url, s.embed_model, s.embed_dim)
    try:
        for name in ("decisions", "counters", "pr_reviews"):
            await db[name].delete_many({})
        await driver.execute_query("MATCH (n) DETACH DELETE n")
        await ensure_schema(driver, s.embed_dim)
        print("Memory wiped.")
        if seed_file:
            with open(seed_file, encoding="utf-8") as f:
                items = json.load(f)
            for item in items:
                d = await save_decision(db, driver, embedder, DecisionIn(**item))
                print(f"  {d.id}  {d.title}" + ("  (needs_sync!)" if d.needs_sync else ""))
            print(f"Seeded {len(items)} decisions.")
    finally:
        await embedder.close()
        await driver.close()
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
