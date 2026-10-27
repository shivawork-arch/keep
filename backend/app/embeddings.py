"""Turn text into vectors with Ollama.

nomic-embed-text is trained with task prefixes: stored text gets "search_document: ",
search text gets "search_query: ". Skipping them makes search noticeably worse.
"""

import httpx

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class EmbeddingError(RuntimeError):
    pass


def decision_text(title: str, decision: str, reason: str, future_implication: str,
                  rejected: str | None = None) -> str:
    """The text that represents a decision for search. Same fields every time, same order."""
    parts = [title, decision]
    if rejected:
        parts.append(f"Rejected: {rejected}")
    parts += [f"Reason: {reason}", f"Rule: {future_implication}"]
    return "\n".join(parts)


class Embedder:
    def __init__(self, base_url: str, model: str, dim: int, client: httpx.AsyncClient | None = None):
        self.url = base_url.rstrip("/") + "/api/embed"
        self.model = model
        self.dim = dim
        self.client = client or httpx.AsyncClient(timeout=30)

    async def _embed(self, text: str) -> list[float]:
        try:
            response = await self.client.post(self.url, json={"model": self.model, "input": text})
            response.raise_for_status()
            vectors = response.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise EmbeddingError(f"Ollama embed failed: {e}") from e
        if len(vectors) != 1 or len(vectors[0]) != self.dim:
            got = len(vectors[0]) if vectors else 0
            raise EmbeddingError(f"Expected 1 vector of size {self.dim}, got size {got}")
        return vectors[0]

    async def embed_document(self, text: str) -> list[float]:
        return await self._embed(DOC_PREFIX + text)

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(QUERY_PREFIX + text)

    async def close(self) -> None:
        await self.client.aclose()
