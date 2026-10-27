"""Search decisions by meaning, not file path (fixes LORE's path-only lookup).

The vector index has no built-in status filter, so we ask for extra candidates,
filter in the query, then cut to k.
"""

from pydantic import BaseModel, Field

from app.embeddings import Embedder
from app.models import Decision, Status
from app.store import VECTOR_INDEX, from_mongo

OVERFETCH = 4
MIN_CANDIDATES = 20

SEARCH_QUERY = f"""
CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $candidates, $vector)
YIELD node, score
WHERE score >= $min_score AND ($statuses IS NULL OR node.status IN $statuses)
OPTIONAL MATCH (node)-[:GOVERNS]->(f:File)
WITH node, score, collect(f.path) AS files
RETURN node.id AS id, score, files
ORDER BY score DESC
LIMIT $k
"""


class SearchRequest(BaseModel):
    text: str = Field(min_length=3)
    k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    include_inactive: bool = False


class SearchHit(BaseModel):
    score: float
    decision: Decision


async def search_decisions(db, driver, embedder: Embedder, req: SearchRequest) -> list[SearchHit]:
    vector = await embedder.embed_query(req.text)
    statuses = None if req.include_inactive else [Status.ACTIVE.value]
    result = await driver.execute_query(
        SEARCH_QUERY,
        candidates=max(req.k * OVERFETCH, MIN_CANDIDATES),
        vector=vector,
        min_score=req.min_score,
        statuses=statuses,
        k=req.k,
    )
    rows = [(r["id"], r["score"]) for r in result.records]
    if not rows:
        return []

    # Full records come from MongoDB, the source of truth.
    docs = {}
    async for doc in db.decisions.find({"_id": {"$in": [i for i, _ in rows]}}):
        docs[doc["_id"]] = from_mongo(doc)
    return [SearchHit(score=s, decision=docs[i]) for i, s in rows if i in docs]
