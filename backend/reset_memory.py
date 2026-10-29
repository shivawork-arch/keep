"""DEV ONLY: wipe KEEP memory (MongoDB + Neo4j), then optionally load a seed file.

Seeding goes through the normal save functions, so seeded records get IDs, vectors and graph
links exactly like real ones. The daily Claude usage counter and the health-report issue are kept.

Usage (from backend/, venv on):
    python reset_memory.py                         # wipe only
    python reset_memory.py ..\\scripts\\seed.json    # wipe, then seed

Seed file: either a list of decisions, or {"decisions": [...], "links": [[from, to], ...], "patterns": [...]}.
"""

import asyncio
import json
import sys

from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.config import Settings
from app.embeddings import Embedder
from app.models import DecisionIn
from app.patterns import PatternIn, save_pattern
from app.store import ensure_schema, link_decisions, save_decision

WIPE = ("decisions", "counters", "pr_reviews", "pr_extractions", "patterns", "incidents", "commit_ledger")


async def main(seed_file: str | None):
    s = Settings()
    mongo = AsyncMongoClient(s.mongo_uri)
    db = mongo["keep"]
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    embedder = Embedder(s.ollama_url, s.embed_model, s.embed_dim)
    try:
        for name in WIPE:
            await db[name].delete_many({})
        await driver.execute_query("MATCH (n) DETACH DELETE n")
        await ensure_schema(driver, s.embed_dim)
        print("Memory wiped.")
        if not seed_file:
            return
        with open(seed_file, encoding="utf-8") as f:
            seed = json.load(f)
        if isinstance(seed, list):
            seed = {"decisions": seed}
        for item in seed.get("decisions", []):
            d = await save_decision(db, driver, embedder, DecisionIn(**item))
            print(f"  {d.id}  {d.title}" + ("  (needs_sync!)" if d.needs_sync else ""))
        for source, target in seed.get("links", []):
            await link_decisions(db, driver, source, target)
            print(f"  {source} depends on {target}")
        for item in seed.get("patterns", []):
            p = await save_pattern(db, driver, PatternIn(**item))
            print(f"  {p.id}  {p.rule}")
        print(f"Seeded {len(seed.get('decisions', []))} decisions, {len(seed.get('links', []))} links, "
              f"{len(seed.get('patterns', []))} rules.")
    finally:
        await embedder.close()
        await driver.close()
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
