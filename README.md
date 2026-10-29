# KEEP

**Knowledge Engine for Engineering Precedent.** KEEP gives a codebase a memory of the decisions its team
made, and uses it to stop the same mistakes coming back.

Teams decide things in PR threads and issue comments: *fixed retry intervals, not exponential backoff,
because of the thundering herd in March*. Six months later someone new opens a PR with exponential
backoff, and nobody remembers why it was banned. KEEP remembers, and says so on the PR.

## What it does

| When | Agent | What happens |
|---|---|---|
| PR opened or pushed | **PR Review** | Checks the change against past decisions (by meaning, not just file path), the linked issue's promises, security triggers, tech drift and coding rules. One Claude call; one comment. |
| PR merged | **Decision Extractor** | Pulls decisions out of the PR discussion and stores them with ID, reason, owners and files. |
| `keep:` reply | **Reply Handler** | `keep: intentional — reason` records an override and supersedes the old rule; `keep: accidental`; `keep: discuss` pings the deciders; `keep: ask …`; `keep: onboard`; `keep: approve KEEP-P-002`. |
| Issue opened | **Pre-Mortem** | Warns which past decisions the work will run into, before code is written. |
| Push | **Commit Keeper** | A ledger entry per push, summarised by a local model (no API cost). |
| Issue labelled `incident` | **Incident Linker** | Records KEEP-I-001 and links it to related decisions; flags rules changed in the last 30 days. |
| Daily | **Follow-Up**, **Health Auditor**, **Decision Decay**, **Guardrail Builder** | Nudge unanswered conflicts; keep a health-report issue; re-check decisions whose code changed a lot; turn feedback repeated in 3+ PRs into proposed coding rules. |
| On demand | **Ask**, **Onboarding**, **Migrate** | Answer questions from memory (also from the dashboard); a brief for new developers; build memory from past PRs. |

The dashboard shows the memory as a graph (files on the left, people on the right, decisions between
them), with decisions, change log, health report and onboarding brief, and an ask box whose answers
light up the decisions they cite.

## How it fits together

```
GitHub event ──> Cloudflare Tunnel ──> FastAPI /github/webhook (signature check)
                                            │
                                            v
                                   n8n router (fixed rules, no AI)
                                            │  one branch per agent
                                            v
                         FastAPI agent endpoints ──> Claude (claude -p or API key)
                            │            │
                        MongoDB       Neo4j (graph + vector search) <── Ollama embeddings
                            │
                     Next.js dashboard (Cytoscape.js)
```

Design rules:
- **Claude only judges and writes text.** IDs, status changes, links, routing and every "is this allowed"
  check live in code. Anything Claude cites that it was not given (a decision ID, a file) is dropped.
- **One write path.** Every write goes to MongoDB (full record) and Neo4j (node, links, vector) through one
  function; if Neo4j fails the record is marked `needs_sync` and `POST /sync/repair` rebuilds it.
- **Cost rules.** No AI routing. Nothing related in memory → no Claude call. Haiku for extraction and
  sorting, Sonnet for review and pre-mortem. One call per PR. Unchanged files are not re-reviewed.
  A daily call/token cap pauses non-urgent jobs. Commit summaries run on a local Ollama model.

## Stack

GitHub · n8n (self-hosted) · FastAPI · MongoDB · Neo4j (with vector index) · Ollama (`nomic-embed-text`,
`llama3.2:3b`) · Claude via `claude -p` or the API · Next.js + Cytoscape.js · pytest + GitHub Actions ·
Cloudflare Tunnel.

## Run it locally (Windows / PowerShell)

Needs Docker Desktop, Python 3.11, Node 20+, git, cloudflared, and Claude Code logged in (subscription
mode) or an Anthropic API key.

```powershell
git clone https://github.com/shivawork-arch/keep.git; cd keep
Copy-Item .env.example .env          # then fill in passwords, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
cd backend; python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; cd ..
docker compose up -d
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec ollama ollama pull llama3.2:3b
powershell -ExecutionPolicy Bypass -File scripts\start.ps1    # services, API, tunnel; prints the webhook URL
```

Then:
1. In the GitHub repo KEEP should watch: Settings → Webhooks → the printed URL, content type
   `application/json`, your secret, events: Issues, Issue comments, Pull requests, Pushes.
2. Import `workflows/github-router.json` into n8n (http://localhost:5678) and publish it.
3. Optional seed data: `cd backend; python reset_memory.py ..\scripts\seed.json`
4. Dashboard: `cd dashboard; npm install; npm run dev` → http://localhost:3000

Tests: `cd backend; pytest -q`.

## Known limits

- Comments are posted with a personal access token, so they appear under that account. A GitHub App is
  the proper fix.
- The free quick tunnel gets a new URL on every restart; a named tunnel on your own domain fixes that.
- In subscription mode, Migrate and Decision Decay make calls one after another (the Batch API is only
  available with an API key).

---

Inspired by LORE (GitLab AI Hackathon 2026). KEEP is a rebuild on a different stack with new agents.
