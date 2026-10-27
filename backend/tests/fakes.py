"""In-memory stand-ins for MongoDB, Neo4j and Ollama, so store tests need no services."""


class FakeCollection:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        if doc["_id"] in self.docs:
            raise ValueError("duplicate _id")
        self.docs[doc["_id"]] = dict(doc)

    async def update_one(self, filter, update):
        doc = self.docs[filter["_id"]]
        doc.update(update.get("$set", {}))
        for field, value in update.get("$addToSet", {}).items():
            items = doc.setdefault(field, [])
            if value not in items:
                items.append(value)
        for field, value in update.get("$push", {}).items():
            doc.setdefault(field, []).append(value)

    async def find_one(self, filter):
        doc = self.docs.get(filter["_id"])
        return dict(doc) if doc else None

    def find(self, filter, projection=None):
        if "_id" in filter:
            ids = set(filter["_id"]["$in"])
            docs = [dict(d) for k, d in self.docs.items() if k in ids]
        else:
            docs = [dict(d) for d in self.docs.values()
                    if all(d.get(k) == v for k, v in filter.items())]

        async def gen():
            for d in docs:
                yield d

        return gen()

    async def find_one_and_update(self, filter, update, upsert, return_document):
        key = filter["_id"]
        doc = self.docs.setdefault(key, {"_id": key, "seq": 0})
        doc["seq"] += update["$inc"]["seq"]
        return dict(doc)


class FakeDB:
    def __init__(self):
        self.decisions = FakeCollection()
        self.counters = FakeCollection()


class FakeResult:
    def __init__(self, records):
        self.records = records


class FakeDriver:
    """Records every query. Remembers Decision ids written, so sync checks can read them back."""

    def __init__(self, fail=False, records=None):
        self.calls = []
        self.fail = fail
        self.records = records or []
        self.graph_ids = set()

    async def execute_query(self, query, **params):
        if self.fail:
            raise ConnectionError("neo4j down")
        self.calls.append((query, params))
        if query.lstrip().startswith("MERGE (d:Decision"):
            self.graph_ids.add(params["id"])
        if "RETURN d.id AS id" in query:
            return FakeResult([{"id": i} for i in sorted(self.graph_ids)])
        return FakeResult(self.records)


class FakeEmbedder:
    def __init__(self, fail=False, dim=4):
        self.fail = fail
        self.dim = dim
        self.texts = []

    async def embed_document(self, text):
        if self.fail:
            from app.embeddings import EmbeddingError
            raise EmbeddingError("ollama down")
        self.texts.append(text)
        return [0.5] * self.dim

    async def embed_query(self, text):
        self.texts.append(("query", text))
        return [0.25] * self.dim
