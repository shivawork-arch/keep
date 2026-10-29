"""Small key/value store for things like 'which issue holds the health report'."""


async def get_state(db, key: str) -> dict:
    return await db.keep_state.find_one({"_id": key}) or {}


async def set_state(db, key: str, **values) -> None:
    await db.keep_state.update_one({"_id": key}, {"$set": values}, upsert=True)
