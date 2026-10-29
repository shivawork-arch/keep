"""Pattern rules (KEEP-P-001): coding standards that came from reviewer feedback.

Stored like decisions: full record in MongoDB, a node in Neo4j, written together.
"""

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.ids import Kind, next_id

log = logging.getLogger("keep.patterns")

LANGUAGES = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
             ".tsx": "typescript", ".go": "go", ".java": "java", ".rb": "ruby", ".rs": "rust",
             ".cs": "csharp", ".php": "php", ".kt": "kotlin", ".swift": "swift"}


def language_of(path: str) -> str | None:
    for ext, lang in LANGUAGES.items():
        if path.endswith(ext):
            return lang
    return None


class PatternIn(BaseModel):
    rule: str = Field(min_length=3)
    anti_pattern: str = Field(min_length=1)
    language: str = Field(min_length=1)
    reason: str = Field(min_length=3)
    bad_example: str | None = None
    good_example: str | None = None
    established_by: list[str] = Field(default_factory=list)
    source_url: str | None = None


class Pattern(PatternIn):
    id: str
    status: str = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    needs_sync: bool = False


PATTERN_NODE = """
MERGE (p:Pattern {id: $id})
SET p.rule = $rule, p.language = $language, p.status = $status
"""


async def save_pattern(db, driver, data: PatternIn, status: str = "active") -> Pattern:
    p = Pattern(id=await next_id(db, Kind.PATTERN), status=status, **data.model_dump())
    doc = p.model_dump(exclude={"id"})
    doc["_id"] = p.id
    await db.patterns.insert_one(doc)
    try:
        await driver.execute_query(PATTERN_NODE, id=p.id, rule=p.rule, language=p.language, status=p.status)
    except Exception as e:
        log.warning("Graph write failed for %s: %s", p.id, e)
        await db.patterns.update_one({"_id": p.id}, {"$set": {"needs_sync": True}})
        p.needs_sync = True
    return p


async def patterns_for(db, languages: set[str]) -> list[Pattern]:
    if not languages:
        return []
    out = []
    async for doc in db.patterns.find({"language": {"$in": sorted(languages)}, "status": "active"}):
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        out.append(Pattern(**doc))
    return out


async def set_pattern_status(db, driver, pattern_id: str, status: str) -> bool:
    doc = await db.patterns.find_one({"_id": pattern_id})
    if not doc:
        return False
    await db.patterns.update_one({"_id": pattern_id}, {"$set": {"status": status}})
    try:
        await driver.execute_query("MATCH (p:Pattern {id: $id}) SET p.status = $status", id=pattern_id, status=status)
    except Exception as e:
        log.warning("Graph status update failed for %s: %s", pattern_id, e)
        await db.patterns.update_one({"_id": pattern_id}, {"$set": {"needs_sync": True}})
    return True
