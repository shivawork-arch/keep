"""The one place that writes decisions. Every write goes to MongoDB and Neo4j together.

MongoDB holds the full record. Neo4j holds a node with the same ID, its links and its
search vector. If the Neo4j side fails, the Mongo record is marked needs_sync=True so a
later sync can repair it (the write is never lost).
"""

import logging
from datetime import datetime, timezone

from app.embeddings import Embedder, decision_text
from app.ids import Kind, next_id
from app.models import Decision, DecisionIn, HistoryEntry

log = logging.getLogger("keep.store")

VECTOR_INDEX = "decision_embedding"

SCHEMA = [
    "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT file_path IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE",
    "CREATE CONSTRAINT person_login IF NOT EXISTS FOR (p:Person) REQUIRE p.login IS UNIQUE",
]



def vector_index_query(dim: int) -> str:
    # Built as text: index options are not reliably parameterisable. dim is a checked int.
    return (
        f"CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS "
        "FOR (d:Decision) ON d.embedding "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dim)}, "
        "`vector.similarity_function`: 'cosine'}}"
    )

# One query writes the node, its vector, its files and its people.
# FOREACH (not UNWIND) so an empty files or people list does not drop the row.
UPSERT_DECISION = """
MERGE (d:Decision {id: $id})
SET d.title = $title,
    d.status = $status,
    d.confidence = $confidence,
    d.security_relevant = $security_relevant,
    d.created_at = datetime($created_at),
    d.embedding = $embedding
FOREACH (path IN $files |
    MERGE (f:File {path: path})
    MERGE (d)-[:GOVERNS]->(f))
FOREACH (login IN $people |
    MERGE (p:Person {login: login})
    MERGE (p)-[:DECIDED]->(d))
"""


async def ensure_schema(driver, dim: int) -> None:
    """Create constraints and the vector index. Safe to run on every startup."""
    for query in SCHEMA:
        await driver.execute_query(query)
    await driver.execute_query(vector_index_query(dim))


def to_mongo(d: Decision) -> dict:
    doc = d.model_dump(mode="python", exclude={"id"})
    doc["_id"] = d.id
    doc["source"]["url"] = str(d.source.url)  # HttpUrl is not BSON-encodable
    return doc


def from_mongo(doc: dict) -> Decision:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    return Decision(**doc)


async def write_graph(driver, d: Decision, embedding: list[float]) -> None:
    await driver.execute_query(
        UPSERT_DECISION,
        id=d.id,
        title=d.title,
        status=d.status.value,
        confidence=d.confidence.value,
        security_relevant=d.security_relevant,
        created_at=d.created_at.isoformat(),
        embedding=embedding,
        files=d.files,
        people=d.decided_by,
    )


async def save_decision(db, driver, embedder: Embedder, data: DecisionIn) -> Decision:
    decision = Decision(
        id=await next_id(db, Kind.DECISION),
        history=[HistoryEntry(at=datetime.now(timezone.utc), change="created")],
        **data.model_dump(),
    )

    # 1. MongoDB first: it is the source of truth. If this fails, nothing was written anywhere.
    await db.decisions.insert_one(to_mongo(decision))

    # 2. Neo4j (needs the vector). Any failure here marks the record for re-sync.
    try:
        embedding = await embedder.embed_document(
            decision_text(data.title, data.decision, data.reason, data.future_implication, data.rejected)
        )
        await write_graph(driver, decision, embedding)
    except Exception as e:
        log.warning("Graph write failed for %s, marking needs_sync: %s", decision.id, e)
        await db.decisions.update_one({"_id": decision.id}, {"$set": {"needs_sync": True}})
        decision.needs_sync = True

    return decision


async def get_decision(db, decision_id: str) -> Decision | None:
    doc = await db.decisions.find_one({"_id": decision_id})
    return from_mongo(doc) if doc else None
