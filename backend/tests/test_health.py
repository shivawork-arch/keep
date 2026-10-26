from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(monkeypatch):
    # No `with` block, so the lifespan (real settings and connections) never runs.
    main.app.state.mongo = MagicMock()
    main.app.state.neo4j = MagicMock()
    main.app.state.settings = SimpleNamespace(ollama_url="http://ollama.test")

    main.app.state.mongo.admin.command = AsyncMock(return_value={"ok": 1})
    main.app.state.neo4j.verify_connectivity = AsyncMock(return_value=None)
    monkeypatch.setattr(main.httpx, "AsyncClient", _ollama_client(200))
    return TestClient(main.app)


def _ollama_client(status_code=200, error=None):
    def handler(request):
        if error:
            raise error
        return httpx.Response(status_code, text="Ollama is running")

    real = httpx.AsyncClient

    def factory(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def test_all_up(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"mongodb": "ok", "neo4j": "ok", "ollama": "ok"}


def test_mongodb_down(client):
    main.app.state.mongo.admin.command.side_effect = Exception("no server")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"mongodb": "down", "neo4j": "ok", "ollama": "ok"}


def test_neo4j_down(client):
    main.app.state.neo4j.verify_connectivity.side_effect = Exception("unreachable")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"mongodb": "ok", "neo4j": "down", "ollama": "ok"}


@pytest.mark.parametrize(
    "factory",
    [_ollama_client(error=httpx.ConnectError("refused")), _ollama_client(status_code=500)],
    ids=["unreachable", "error-status"],
)
def test_ollama_down(client, monkeypatch, factory):
    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"mongodb": "ok", "neo4j": "ok", "ollama": "down"}


def test_settings_missing_field_fails(monkeypatch, tmp_path):
    from pydantic import ValidationError

    from app.config import Settings

    for name in ("MONGO_URI", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "OLLAMA_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=tmp_path / "missing.env")
