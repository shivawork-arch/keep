"""Find the vector-score floor (review_min_score) that separates related from unrelated changes.

Runs real searches against your memory (Ollama + Neo4j), then suggests the midpoint between the
weakest related match and the strongest unrelated one. Usage (from backend/, venv on):
    python tune_cutoff.py
"""

import asyncio

from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient

from app.config import Settings
from app.embeddings import Embedder
from app.search import SearchRequest, search_decisions

# (text that looks like a PR or issue, ID it should match, or None if nothing should match)
CASES = [
    ("Retry the charge with a delay that doubles after each failed attempt", "KEEP-D-001"),
    ("Add jittered backoff multiplier to the shipping client retries", "KEEP-D-001"),
    ("Keep product prices in a module-level dict so lookups skip Redis", "KEEP-D-002"),
    ("Store the password as sha256(salt + password) for faster login", "KEEP-D-003"),
    ("Save the JWT in localStorage so the SPA can read it", "KEEP-D-004"),
    ("Calculate the discount with float math: price * 0.85", "KEEP-D-005"),
    ("Charge the card again if the provider times out", "KEEP-D-006"),
    ("Orders list endpoint: page and per_page using OFFSET", "KEEP-D-007"),
    ("Use datetime.now() for the order created time", "KEEP-D-008"),
    ("Rename the column user_name to username in one migration", "KEEP-D-009"),
    ("Add the requests library to call the tax API", "KEEP-D-010"),
    ("Call the shipping API with requests.get(url) and no timeout", "KEEP-D-011"),
    ("Fix typo in the README introduction", None),
    ("Change the footer colour to dark grey and bump the logo size", None),
    ("Add a slugify helper for product names", None),
    ("Update the copyright year in the license file", None),
    ("Translate the checkout button label into Arabic", None),
]


async def main():
    s = Settings()
    mongo = AsyncMongoClient(s.mongo_uri)
    db = mongo["keep"]
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    embedder = Embedder(s.ollama_url, s.embed_model, s.embed_dim)
    related, unrelated, misses = [], [], []
    try:
        for text, expected in CASES:
            hits = await search_decisions(db, driver, embedder, SearchRequest(text=text, k=3))
            top = hits[0] if hits else None
            score = top.score if top else 0.0
            ids = [h.decision.id for h in hits]
            if expected:
                rank = ids.index(expected) + 1 if expected in ids else None
                exp_score = next((h.score for h in hits if h.decision.id == expected), 0.0)
                related.append(exp_score)
                if rank != 1:
                    misses.append((text, expected, ids))
                print(f"{exp_score:.3f}  expect {expected}  rank {rank or '-'}  | {text[:60]}")
            else:
                unrelated.append(score)
                print(f"{score:.3f}  expect none        top {ids[0] if ids else '-'} | {text[:60]}")
    finally:
        await embedder.close()
        await driver.close()
        await mongo.close()

    lo, hi = min(related), max(unrelated)
    print(f"\nWeakest related match:    {lo:.3f}")
    print(f"Strongest unrelated match: {hi:.3f}")
    if lo > hi:
        print(f"Clean gap. Suggested REVIEW_MIN_SCORE={(lo + hi) / 2:.3f}")
    else:
        print(f"Overlap: some unrelated text scores above a related one. A floor of {hi:.3f} would hide "
              f"{sum(r <= hi for r in related)} related match(es); keep the floor low and let Claude judge.")
    if misses:
        print("\nNot ranked first (search found something else first):")
        for text, exp, ids in misses:
            print(f"  {exp}: got {ids} for '{text[:50]}'")


if __name__ == "__main__":
    asyncio.run(main())
