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


class LinkError(ValueError):
    pass


LINK_QUERY = """
MATCH (a:Decision {id: $source}), (b:Decision {id: $target})
MERGE (a)-[:DEPENDS_ON]->(b)
"""


async def _depends_on_path(db, start: str, goal: str) -> bool:
    """True if `start` already depends on `goal`, directly or through other decisions."""
    seen, stack = set(), [start]
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        doc = await db.decisions.find_one({"_id": current})
        if doc:
            stack.extend(doc.get("depends_on", []))
    return False


async def link_decisions(db, driver, source: str, target: str) -> Decision:
    """Record that `source` depends on `target` (so `target` blocks `source`).

    Both sides are written together; the graph holds one DEPENDS_ON edge.
    """
    if source == target:
        raise LinkError("A decision cannot depend on itself")
    a = await db.decisions.find_one({"_id": source})
    b = await db.decisions.find_one({"_id": target})
    missing = [i for i, doc in ((source, a), (target, b)) if doc is None]
    if missing:
        raise LinkError(f"Not found: {', '.join(missing)}")
    if target in a.get("depends_on", []):
        return from_mongo(a)  # already linked; nothing to do
    if await _depends_on_path(db, target, source):
        raise LinkError(f"Loop: {target} already depends on {source}")

    now = datetime.now(timezone.utc)
    await db.decisions.update_one(
        {"_id": source},
        {"$addToSet": {"depends_on": target},
         "$push": {"history": {"at": now, "change": f"depends on {target}"}}},
    )
    await db.decisions.update_one(
        {"_id": target},
        {"$addToSet": {"blocks": source},
         "$push": {"history": {"at": now, "change": f"blocks {source}"}}},
    )

    try:
        await driver.execute_query(LINK_QUERY, source=source, target=target)
    except Exception as e:
        log.warning("Graph link failed %s -> %s, marking needs_sync: %s", source, target, e)
        for i in (source, target):
            await db.decisions.update_one({"_id": i}, {"$set": {"needs_sync": True}})

    return await get_decision(db, source)


STATUS_QUERY = "MATCH (d:Decision {id: $id}) SET d.status = $status"
SUPERSEDES_QUERY = """
MATCH (new:Decision {id: $new}), (old:Decision {id: $old})
MERGE (new)-[:SUPERSEDES]->(old)
"""


async def supersede(db, driver, old_id: str, new_id: str) -> None:
    """Mark old as superseded by new, and move old's links onto new (fixed rule, in code)."""
    old = await get_decision(db, old_id)
    if old is None:
        raise LinkError(f"Not found: {old_id}")
    now = datetime.now(timezone.utc)
    await db.decisions.update_one(
        {"_id": old_id},
        {"$set": {"status": "superseded"},
         "$push": {"history": {"at": now, "change": f"superseded by {new_id}"}}},
    )
    await db.decisions.update_one(
        {"_id": new_id}, {"$push": {"history": {"at": now, "change": f"overrides {old_id}"}}})
    # Links move: what old depended on, new now depends on; what depended on old now depends on new.
    for target in old.depends_on:
        if target != new_id:
            await link_decisions(db, driver, new_id, target)
    for dependent in old.blocks:
        if dependent != new_id:
            try:
                await link_decisions(db, driver, dependent, new_id)
            except LinkError as e:
                log.warning("Could not move link %s -> %s: %s", dependent, new_id, e)
    try:
        await driver.execute_query(STATUS_QUERY, id=old_id, status="superseded")
        await driver.execute_query(SUPERSEDES_QUERY, new=new_id, old=old_id)
    except Exception as e:
        log.warning("Graph supersede failed %s -> %s: %s", new_id, old_id, e)
        for i in (old_id, new_id):
            await db.decisions.update_one({"_id": i}, {"$set": {"needs_sync": True}})
