"""Every Claude call in KEEP goes through ask_claude().

Two modes, one setting (claude_mode):
  - "subscription": runs `claude -p` (Claude Code CLI) with tools off, so Claude only judges/writes.
  - "api": calls the Messages API with an API key, with prompt caching on the system prompt.

Model is picked per job (cost rules): Haiku for sorting/extraction, Sonnet for review/pre-mortem.
A daily counter in MongoDB pauses non-urgent jobs once the call or token cap is reached.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from enum import Enum

import httpx
from pydantic import BaseModel


class Tier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"


# Job -> model. Opus is deliberately absent: it needs explicit approval per job.
JOB_MODELS: dict[str, Tier] = {
    "pr_review": Tier.SONNET,
    "pre_mortem": Tier.SONNET,
    "decision_extract": Tier.HAIKU,
    "reply_parse": Tier.HAIKU,
    "override_write": Tier.HAIKU,
    "sort": Tier.HAIKU,
    "ping": Tier.HAIKU,
}

# API mode needs full model names; the CLI accepts the short aliases.
API_MODELS = {Tier.HAIKU: "claude-haiku-5-5", Tier.SONNET: "claude-sonnet-5-5"}

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class ClaudeError(RuntimeError):
    pass


class BudgetExceeded(ClaudeError):
    pass


class ClaudeResult(BaseModel):
    text: str
    data: dict | None = None
    model: str
    mode: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


# ---------- daily budget ----------

def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Budget:
    def __init__(self, db, call_cap: int, token_cap: int):
        self.db, self.call_cap, self.token_cap = db, call_cap, token_cap

    async def usage(self) -> dict:
        doc = await self.db.usage.find_one({"_id": today()}) or {}
        return {"date": today(), "calls": doc.get("calls", 0), "tokens": doc.get("tokens", 0),
                "cost_usd": round(doc.get("cost_usd", 0.0), 4),
                "call_cap": self.call_cap, "token_cap": self.token_cap}

    async def check(self, urgent: bool) -> None:
        if urgent:
            return  # urgent jobs (e.g. PR review) still run; they are still counted
        u = await self.usage()
        if u["calls"] >= self.call_cap or u["tokens"] >= self.token_cap:
            raise BudgetExceeded(f"Daily cap reached ({u['calls']} calls, {u['tokens']} tokens)")

    async def record(self, r: ClaudeResult) -> None:
        await self.db.usage.update_one(
            {"_id": today()},
            {"$inc": {"calls": 1, "tokens": r.input_tokens + r.output_tokens,
                      "cost_usd": r.cost_usd or 0.0}},
            upsert=True,
        )


# ---------- subscription mode (claude -p) ----------

def cli_args(cli: str, tier: Tier, system_file: str, schema: dict | None) -> list[str]:
    args = [cli, "-p", "--output-format", "json", "--model", tier.value,
            "--tools", "", "--system-prompt-file", system_file,
            "--no-session-persistence", "--strict-mcp-config"]
    if schema:
        args += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    return args


def parse_cli_output(stdout: str, tier: Tier, want_json: bool) -> ClaudeResult:
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"claude -p did not return JSON: {stdout[:200]!r}") from e
    if out.get("is_error"):
        raise ClaudeError(f"claude -p failed: {out.get('result')}")
    if want_json and "structured_output" not in out:
        raise ClaudeError("claude -p returned no structured_output (declined or schema not met)")
    usage = out.get("usage") or {}
    return ClaudeResult(
        text=out.get("result") or "",
        data=out.get("structured_output") if want_json else None,
        model=tier.value, mode="subscription",
        input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cost_usd=out.get("total_cost_usd"),
    )


def _run_cli(args: list[str], prompt: str, timeout: int) -> subprocess.CompletedProcess:
    # Empty working folder: claude -p must not load this repo's CLAUDE.md, hooks or MCP config.
    with tempfile.TemporaryDirectory(prefix="keep-claude-") as cwd:
        return subprocess.run(args, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, cwd=cwd)


async def call_cli(cli: str, tier: Tier, system: str, prompt: str, schema: dict | None,
                   timeout: int) -> ClaudeResult:
    exe = shutil.which(cli)
    if not exe:
        raise ClaudeError(f"'{cli}' not found on PATH. Set CLAUDE_CLI to its full path.")
    fd, system_file = tempfile.mkstemp(prefix="keep-system-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        # A thread, not asyncio subprocesses: those fail on some Windows event loops.
        proc = await asyncio.to_thread(_run_cli, cli_args(exe, tier, system_file, schema), prompt, timeout)
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"claude -p timed out after {timeout}s") from e
    finally:
        os.remove(system_file)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise ClaudeError(f"claude -p exited {proc.returncode}: {proc.stderr[:300]}")
    return parse_cli_output(proc.stdout, tier, want_json=schema is not None)


# ---------- API mode ----------

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def api_body(tier: Tier, system: str, prompt: str, schema: dict | None, max_tokens: int) -> dict:
    if schema:
        system += ("\n\nReply with only a JSON object that matches this JSON Schema. "
                   "No prose, no code fences.\n" + json.dumps(schema))
    return {
        "model": API_MODELS[tier],
        "max_tokens": max_tokens,
        # cache_control: the shared system prompt is cached across calls (cost rule).
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
    }


def parse_api_response(body: dict, tier: Tier, want_json: bool) -> ClaudeResult:
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    data = None
    if want_json:
        try:
            data = json.loads(FENCE.sub("", text.strip()))
        except json.JSONDecodeError as e:
            raise ClaudeError(f"API reply was not JSON: {text[:200]!r}") from e
        if not isinstance(data, dict):
            raise ClaudeError("API reply JSON was not an object")
    u = body.get("usage") or {}
    return ClaudeResult(
        text=text, data=data, model=API_MODELS[tier], mode="api",
        input_tokens=u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
    )


async def call_api(api_key: str, tier: Tier, system: str, prompt: str, schema: dict | None,
                   timeout: int, max_tokens: int = 4096,
                   client: httpx.AsyncClient | None = None) -> ClaudeResult:
    if not api_key:
        raise ClaudeError("claude_mode is 'api' but ANTHROPIC_API_KEY is not set")
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        r = await client.post(API_URL, headers=headers, json=api_body(tier, system, prompt, schema, max_tokens))
    except httpx.HTTPError as e:
        raise ClaudeError(f"API request failed: {e}") from e
    finally:
        if owns:
            await client.aclose()
    if r.status_code != 200:
        raise ClaudeError(f"API returned {r.status_code}: {r.text[:300]}")
    return parse_api_response(r.json(), tier, want_json=schema is not None)


# ---------- the one entry point ----------

async def ask_claude(settings, db, job: str, system: str, prompt: str,
                     schema: dict | None = None, urgent: bool = False) -> ClaudeResult:
    if job not in JOB_MODELS:
        raise ClaudeError(f"Unknown job {job!r}; add it to JOB_MODELS with a model tier")
    tier = JOB_MODELS[job]
    budget = Budget(db, settings.daily_call_cap, settings.daily_token_cap)
    await budget.check(urgent)

    if settings.claude_mode == "subscription":
        result = await call_cli(settings.claude_cli, tier, system, prompt, schema,
                                settings.claude_timeout_seconds)
    elif settings.claude_mode == "api":
        result = await call_api(settings.anthropic_api_key, tier, system, prompt, schema,
                                settings.claude_timeout_seconds)
    else:
        raise ClaudeError(f"claude_mode must be 'subscription' or 'api', got {settings.claude_mode!r}")

    await budget.record(result)
    return result
