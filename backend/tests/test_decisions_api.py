import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fakes import FakeDB, FakeDriver, FakeEmbedder

BODY = {
    "title": "Fixed retry intervals",
    "decision": "Use fixed retry intervals",
    "rejected": "Exponential backoff",
    "reason": "Thundering herd at 1000+ concurrent requests",
    "future_implication": "No exponential backoff in retry logic",
    "source": {"type": "pr", "url": "https://github.com/o/r/pull/42", "ref": "42"},
    "decided_by": ["alice"],
    "files": ["src/api/retry.py"],
}


@pytest.fixture
def client():
    # No `with` block, so the real lifespan never runs.
    main.app.state.db = FakeDB()
    main.app.state.neo4j = FakeDriver()
    main.app.state.embedder = FakeEmbedder()
    return TestClient(main.app)


def test_create_then_read(client):
    created = client.post("/decisions", json=BODY)
    assert created.status_code == 201
    assert created.json()["id"] == "KEEP-D-001"
    assert created.json()["status"] == "active"

    read = client.get("/decisions/KEEP-D-001")
    assert read.status_code == 200
    assert read.json()["title"] == BODY["title"]


def test_caller_cannot_set_id_or_status(client):
    body = {**BODY, "id": "KEEP-D-999", "status": "superseded"}
    created = client.post("/decisions", json=body).json()
    assert created["id"] == "KEEP-D-001"
    assert created["status"] == "active"


def test_invalid_body_is_422(client):
    assert client.post("/decisions", json={**BODY, "reason": ""}).status_code == 422


def test_missing_is_404(client):
    assert client.get("/decisions/KEEP-D-404").status_code == 404


@pytest.mark.parametrize("bad", ["123", "LORE-001", "KEEP-P-001"])
def test_bad_id_is_400(client, bad):
    assert client.get(f"/decisions/{bad}").status_code == 400
