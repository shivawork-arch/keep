import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import claude
from app.claude import (Budget, BudgetExceeded, ClaudeError, ClaudeResult, Tier, api_body, ask_claude,
                        call_api, cli_args, parse_api_response, parse_cli_output)
from tests.fakes import FakeDB

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def run(c):
    return asyncio.run(c)


def settings(**kw):
    base = dict(claude_mode="subscription", claude_cli="claude", anthropic_api_key=None,
                claude_timeout_seconds=30, daily_call_cap=2, daily_token_cap=1000)
    base.update(kw)
    return SimpleNamespace(**base)


# --- CLI mode ---

def test_cli_args_turn_tools_off_and_pick_model():
    args = cli_args("claude", Tier.HAIKU, "sys.txt", None)
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--output-format") + 1] == "json"
    assert "--json-schema" not in args


def test_cli_args_with_schema():
    args = cli_args("claude", Tier.SONNET, "sys.txt", SCHEMA)
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA


def test_parse_cli_text_result():
    out = json.dumps({"result": "OK", "is_error": False, "total_cost_usd": 0.0012,
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 2}})
    r = parse_cli_output(out, Tier.HAIKU, want_json=False)
    assert (r.text, r.input_tokens, r.output_tokens, r.cost_usd) == ("OK", 15, 2, 0.0012)


def test_parse_cli_structured():
    out = json.dumps({"result": "", "is_error": False, "structured_output": {"ok": True}, "usage": {}})
    assert parse_cli_output(out, Tier.SONNET, want_json=True).data == {"ok": True}


def test_parse_cli_missing_structured_is_error():
    out = json.dumps({"result": "I can't", "is_error": False, "usage": {}})
    with pytest.raises(ClaudeError, match="structured_output"):
        parse_cli_output(out, Tier.SONNET, want_json=True)


def test_parse_cli_is_error():
    with pytest.raises(ClaudeError, match="Not logged in"):
        parse_cli_output(json.dumps({"is_error": True, "result": "Not logged in"}), Tier.HAIKU, False)


def test_parse_cli_not_json():
    with pytest.raises(ClaudeError):
        parse_cli_output("Error: boom", Tier.HAIKU, False)


# --- API mode ---

def test_api_body_caches_system_and_adds_schema():
    body = api_body(Tier.SONNET, "be dry", "review this", SCHEMA, 1000)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert '"required": ["ok"]' in body["system"][0]["text"]
    assert body["messages"] == [{"role": "user", "content": "review this"}]


def test_parse_api_strips_code_fence():
    body = {"content": [{"type": "text", "text": '```json\n{"ok": true}\n```'}],
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 5}}
    r = parse_api_response(body, Tier.HAIKU, want_json=True)
    assert r.data == {"ok": True}
    assert r.input_tokens == 1000


def test_parse_api_bad_json():
    with pytest.raises(ClaudeError):
        parse_api_response({"content": [{"type": "text", "text": "nope"}]}, Tier.HAIKU, True)


def test_call_api_sends_key_and_version():
    seen = {}

    def handler(req):
        seen["key"] = req.headers["x-api-key"]
        seen["version"] = req.headers["anthropic-version"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}],
                                         "usage": {"input_tokens": 3, "output_tokens": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(call_api("sk-test", Tier.HAIKU, "s", "p", None, 10, client=client))
    assert r.text == "OK" and r.mode == "api"
    assert seen == {"key": "sk-test", "version": "2023-06-01"}


def test_call_api_error_status():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(ClaudeError, match="401"):
        run(call_api("sk", Tier.HAIKU, "s", "p", None, 10, client=client))


def test_call_api_needs_key():
    with pytest.raises(ClaudeError, match="ANTHROPIC_API_KEY"):
        run(call_api("", Tier.HAIKU, "s", "p", None, 10))


# --- budget + entry point ---

def fake_result(**kw):
    return ClaudeResult(text="OK", model="haiku", mode="subscription",
                        input_tokens=kw.get("i", 100), output_tokens=kw.get("o", 10), cost_usd=0.01)


def patch_cli(monkeypatch, result=None):
    calls = []

    async def fake_cli(cli, tier, system, prompt, schema, timeout):
        calls.append(tier)
        return result or fake_result()

    monkeypatch.setattr(claude, "call_cli", fake_cli)
    return calls


def test_ask_records_usage(monkeypatch):
    patch_cli(monkeypatch)
    db = FakeDB()
    run(ask_claude(settings(), db, "ping", "s", "p"))
    u = run(Budget(db, 2, 1000).usage())
    assert (u["calls"], u["tokens"], u["cost_usd"]) == (1, 110, 0.01)


def test_job_picks_model(monkeypatch):
    calls = patch_cli(monkeypatch)
    run(ask_claude(settings(), FakeDB(), "pr_review", "s", "p"))
    run(ask_claude(settings(), FakeDB(), "decision_extract", "s", "p"))
    assert calls == [Tier.SONNET, Tier.HAIKU]


def test_unknown_job_rejected(monkeypatch):
    patch_cli(monkeypatch)
    with pytest.raises(ClaudeError, match="JOB_MODELS"):
        run(ask_claude(settings(), FakeDB(), "opus_everything", "s", "p"))


def test_call_cap_pauses_non_urgent_only(monkeypatch):
    patch_cli(monkeypatch)
    db, s = FakeDB(), settings(daily_call_cap=2)
    run(ask_claude(s, db, "ping", "s", "p"))
    run(ask_claude(s, db, "ping", "s", "p"))
    with pytest.raises(BudgetExceeded):
        run(ask_claude(s, db, "ping", "s", "p"))
    run(ask_claude(s, db, "pr_review", "s", "p", urgent=True))  # urgent still runs
    assert run(Budget(db, 2, 1000).usage())["calls"] == 3


def test_token_cap(monkeypatch):
    patch_cli(monkeypatch, fake_result(i=900, o=200))
    db, s = FakeDB(), settings(daily_call_cap=100, daily_token_cap=1000)
    run(ask_claude(s, db, "ping", "s", "p"))
    with pytest.raises(BudgetExceeded, match="tokens"):
        run(ask_claude(s, db, "ping", "s", "p"))


def test_new_day_resets(monkeypatch):
    patch_cli(monkeypatch)
    db, s = FakeDB(), settings(daily_call_cap=1)
    monkeypatch.setattr(claude, "today", lambda: "2026-10-28")
    run(ask_claude(s, db, "ping", "s", "p"))
    monkeypatch.setattr(claude, "today", lambda: "2026-10-29")
    run(ask_claude(s, db, "ping", "s", "p"))  # no BudgetExceeded


def test_mode_switch_to_api(monkeypatch):
    used = []

    async def fake_api(key, tier, system, prompt, schema, timeout):
        used.append(key)
        return fake_result()

    monkeypatch.setattr(claude, "call_api", fake_api)
    patch_cli(monkeypatch)
    run(ask_claude(settings(claude_mode="api", anthropic_api_key="sk-x"), FakeDB(), "ping", "s", "p"))
    assert used == ["sk-x"]


def test_bad_mode(monkeypatch):
    with pytest.raises(ClaudeError, match="claude_mode"):
        run(ask_claude(settings(claude_mode="magic"), FakeDB(), "ping", "s", "p"))
