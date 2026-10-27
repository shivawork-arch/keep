"""In-memory stand-ins for MongoDB, Neo4j and Ollama, so store tests need no services."""


class FakeCollection:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        if doc["_id"] in self.docs:
            raise ValueError("duplicate _id")
        self.docs[doc["_id"]] = dict(doc)

    async def update_one(self, filter, update):
        self.docs[filter["_id"]].update(update["$set"])

    async def find_one(self, filter):
        doc = self.docs.get(filter["_id"])
        return dict(doc) if doc else None

    def find(self, filter):
        ids = set(filter["_id"]["$in"])
        docs = [dict(d) for k, d in self.docs.items() if k in ids]

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
    def __init__(self, fail=False, records=None):
        self.calls = []
        self.fail = fail
        self.records = records or []

    async def execute_query(self, query, **params):
        if self.fail:
            raise ConnectionError("neo4j down")
        self.calls.append((query, params))
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
