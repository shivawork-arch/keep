"""Front door for GitHub events: check the signature, then hand the event to n8n.

GitHub signs the exact request bytes with the shared secret (HMAC-SHA256) and sends
the result in X-Hub-Signature-256. Anything unsigned or wrongly signed is rejected
before n8n ever sees it.
"""

import hashlib
import hmac

import httpx

FORWARD_HEADERS = ("x-github-event", "x-github-delivery", "x-github-hook-id", "content-type", "user-agent")


def signature_for(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_valid_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    # compare_digest takes the same time whether it matches or not (no timing leaks).
    return hmac.compare_digest(signature_for(secret, body), header)


async def forward_to_n8n(url: str, body: bytes, headers: dict[str, str],
                         client: httpx.AsyncClient | None = None) -> int:
    """Pass the original bytes and GitHub headers on unchanged. Returns n8n's status code."""
    keep = {k: v for k, v in headers.items() if k.lower() in FORWARD_HEADERS}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=5)
    try:
        response = await client.post(url, content=body, headers=keep)
        return response.status_code
    finally:
        if owns_client:
            await client.aclose()
