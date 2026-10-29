"""Minimal GitHub REST client: read a PR and its changed files, post a comment."""

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None, client: httpx.AsyncClient | None = None):
        if not token:
            raise GitHubError("GITHUB_TOKEN is not set in .env")
        self.client = client or httpx.AsyncClient(timeout=20)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _req(self, method: str, path: str, **kw):
        r = await self.client.request(method, API + path, headers=self.headers, **kw)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    async def pull(self, repo: str, number: int) -> dict:
        return await self._req("GET", f"/repos/{repo}/pulls/{number}")

    async def pull_files(self, repo: str, number: int) -> list[dict]:
        files, page = [], 1
        while True:
            batch = await self._req("GET", f"/repos/{repo}/pulls/{number}/files",
                                    params={"per_page": 100, "page": page})
            files += batch
            if len(batch) < 100 or page >= 30:  # GitHub caps this list at 3000 files
                return files
            page += 1

    async def issue_comments(self, repo: str, number: int) -> list[dict]:
        return await self._req("GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})

    async def review_comments(self, repo: str, number: int) -> list[dict]:
        return await self._req("GET", f"/repos/{repo}/pulls/{number}/comments", params={"per_page": 100})

    async def issue(self, repo: str, number: int) -> dict:
        return await self._req("GET", f"/repos/{repo}/issues/{number}")

    async def compare(self, repo: str, base: str, head: str) -> dict:
        return await self._req("GET", f"/repos/{repo}/compare/{base}...{head}")

    async def commit(self, repo: str, sha: str) -> dict:
        return await self._req("GET", f"/repos/{repo}/commits/{sha}")

    async def comment(self, repo: str, number: int, body: str) -> dict:
        return await self._req("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def close(self):
        await self.client.aclose()
