import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.github_webhook import forward_to_n8n, is_valid_signature, signature_for

SECRET = "test-secret"
BODY = json.dumps({"action": "opened", "sender": {"type": "User"}}).encode()


def test_known_signature():
    # Example from GitHub's docs: secret "It's a Secret to Everybody", payload "Hello, World!"
    assert signature_for("It's a Secret to Everybody", b"Hello, World!") == (
        "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    )


def test_valid_and_invalid_signatures():
    good = signature_for(SECRET, BODY)
    assert is_valid_signature(SECRET, BODY, good)
    assert not is_valid_signature(SECRET, BODY, None)
    assert not is_valid_signature(SECRET, BODY, "sha256=" + "0" * 64)
    assert not is_valid_signature(SECRET, BODY + b" ", good)  # one changed byte fails
    assert not is_valid_signature("", BODY, good)


def test_forward_keeps_bytes_and_github_headers():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json",
               "X-Hub-Signature-256": "sha256=abc", "Cookie": "nope"}
    status = asyncio.run(forward_to_n8n("http://n8n/webhook/github", BODY, headers, client))
    assert status == 200
    assert seen["body"] == BODY
    assert seen["headers"]["x-github-event"] == "issues"
    assert "cookie" not in seen["headers"]


@pytest.fixture
def client(monkeypatch):
    main.app.state.settings = SimpleNamespace(github_webhook_secret=SECRET,
                                              n8n_router_url="http://n8n/webhook/github")
    calls = []

    async def fake_forward(url, body, headers, client=None):
        calls.append((url, body, headers))
        return 200

    monkeypatch.setattr(main, "forward_to_n8n", fake_forward)
    c = TestClient(main.app)
    c.calls = calls
    return c


def post(client, body, signature):
    headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
    if signature:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/github/webhook", content=body, headers=headers)


def test_signed_event_is_forwarded(client):
    r = post(client, BODY, signature_for(SECRET, BODY))
    assert r.status_code == 202
    (url, body, _), = client.calls
    assert body == BODY


def test_unsigned_event_rejected(client):
    assert post(client, BODY, None).status_code == 401
    assert client.calls == []


def test_wrong_secret_rejected(client):
    assert post(client, BODY, signature_for("other", BODY)).status_code == 401
    assert client.calls == []


def test_n8n_down_is_502(client, monkeypatch):
    async def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(main, "forward_to_n8n", down)
    assert post(client, BODY, signature_for(SECRET, BODY)).status_code == 502


def test_n8n_error_status_is_502(client, monkeypatch):
    async def err(*a, **k):
        return 404

    monkeypatch.setattr(main, "forward_to_n8n", err)
    assert post(client, BODY, signature_for(SECRET, BODY)).status_code == 502
