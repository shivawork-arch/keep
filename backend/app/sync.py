"""Keep MongoDB and Neo4j in step. MongoDB is the source of truth; Neo4j is rebuilt from it."""

import logging

from pydantic import BaseModel

from app.embeddings import Embedder, decision_text
from app.store import LINK_QUERY, from_mongo, write_graph

log = logging.getLogger("keep.sync")

GRAPH_IDS = "MATCH (d:Decision) RETURN d.id AS id"


class SyncReport(BaseModel):
    only_in_mongo: list[str]
    only_in_neo4j: list[str]
    needs_sync: list[str]

    @property
    def in_step(self) -> bool:
        return not (self.only_in_mongo or self.only_in_neo4j or self.needs_sync)


class RepairResult(BaseModel):
    repaired: list[str]
    failed: dict[str, str]


async def sync_check(db, driver) -> SyncReport:
    mongo_ids, flagged = set(), set()
    async for doc in db.decisions.find({}, {"_id": 1, "needs_sync": 1}):
        mongo_ids.add(doc["_id"])
        if doc.get("needs_sync"):
            flagged.add(doc["_id"])
    result = await driver.execute_query(GRAPH_IDS)
    graph_ids = {r["id"] for r in result.records}
    return SyncReport(
        only_in_mongo=sorted(mongo_ids - graph_ids),
        only_in_neo4j=sorted(graph_ids - mongo_ids),
        needs_sync=sorted(flagged),
    )


async def repair(db, driver, embedder: Embedder) -> RepairResult:
    """Rebuild the graph side of every record that is flagged or missing from Neo4j.

    Nodes that exist only in Neo4j are reported by sync_check but never deleted here.
    """
    report = await sync_check(db, driver)
    todo = sorted(set(report.only_in_mongo) | set(report.needs_sync))
    repaired, failed = [], {}
    for decision_id in todo:
        try:
            d = from_mongo(await db.decisions.find_one({"_id": decision_id}))
            embedding = await embedder.embed_document(
                decision_text(d.title, d.decision, d.reason, d.future_implication, d.rejected)
            )
            await write_graph(driver, d, embedding)
            for target in d.depends_on:
                await driver.execute_query(LINK_QUERY, source=d.id, target=target)
            await db.decisions.update_one({"_id": d.id}, {"$set": {"needs_sync": False}})
            repaired.append(d.id)
        except Exception as e:
            log.warning("Repair failed for %s: %s", decision_id, e)
            failed[decision_id] = str(e)
    return RepairResult(repaired=repaired, failed=failed)
