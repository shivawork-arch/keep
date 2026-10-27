import asyncio
import json

import httpx
import pytest

from app.embeddings import Embedder, EmbeddingError, decision_text

DIM = 4


def make_embedder(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Embedder("http://ollama:11434/", "nomic-embed-text", DIM, client=client)


def test_document_and_query_get_their_prefixes():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["input"])
        assert request.url.path == "/api/embed"
        assert body["model"] == "nomic-embed-text"
        return httpx.Response(200, json={"embeddings": [[0.1] * DIM]})

    e = make_embedder(handler)
    assert asyncio.run(e.embed_document("retry")) == [0.1] * DIM
    asyncio.run(e.embed_query("backoff"))
    assert seen == ["search_document: retry", "search_query: backoff"]


def test_wrong_size_vector_is_an_error():
    e = make_embedder(lambda r: httpx.Response(200, json={"embeddings": [[0.1] * 3]}))
    with pytest.raises(EmbeddingError, match="size 4"):
        asyncio.run(e.embed_document("x"))


def test_server_error_is_an_error():
    e = make_embedder(lambda r: httpx.Response(500))
    with pytest.raises(EmbeddingError):
        asyncio.run(e.embed_document("x"))


def test_unreachable_is_an_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingError):
        asyncio.run(make_embedder(handler).embed_document("x"))


def test_decision_text_includes_rejected_only_when_set():
    with_rej = decision_text("T", "D", "R", "F", rejected="X")
    without = decision_text("T", "D", "R", "F")
    assert "Rejected: X" in with_rej
    assert "Rejected" not in without
    assert without == "T\nD\nReason: R\nRule: F"
