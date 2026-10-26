# KEEP

KEEP (Knowledge Engine for Engineering Precedent) gives a codebase memory of past team decisions and warns developers before they repeat old mistakes. It captures decisions automatically from GitHub issues, pull requests, comments, and commits, stores them with IDs, links, owners, and reasons, and checks new code against that memory — on new issues (pre-mortem), on PR open (5-layer review: memory conflicts, promise check, security, code intelligence, pattern rules), on override replies (`keep:` commands), on merge (decision extraction), and on request (ask, health audit, onboarding brief, dashboard).

## Tech stack

| Job | Choice | Why |
|---|---|---|
| Git platform | GitHub | Most users, easy webhooks, GitHub Actions |
| Workflows (event routing, agent steps, schedules) | n8n, self-hosted | Build each workflow visually, like n8n flows. Has a GitHub trigger, HTTP steps and a command step |
| Backend service | FastAPI (Python) | Holds memory, search and graph logic as endpoints that n8n calls |
| Claude access | Both, switchable | Subscription via `claude -p` for personal building; API key mode for when others use it. One setting switches |
| Memory records | MongoDB | Stores full decision, pattern rule, ledger and spec records |
| Knowledge graph | Neo4j | Nodes and links between decisions, files, people, PRs, incidents |
| Search by meaning | Neo4j vector index | Find similar decisions and follow their links in one query |
| Graph drawing | Cytoscape.js | "Wine and cheese" layout: files pinned left, people pinned right, decisions clustered in the middle |
| Dashboard | Next.js + React | Best fit for Cytoscape.js |
| Hosting | Local first, deploy later | Choose a host once it works |
| Tunnel for GitHub webhooks while local | Cloudflare Tunnel | Free. Quick tunnel URL changes on restart; a stable URL needs a named tunnel and a domain on Cloudflare |
| Tests | pytest | Backend logic |
| CI | GitHub Actions | Built into GitHub |
| Settings and secrets | `.env` + Pydantic Settings | Typed, checked at startup |
| Embedding model (turns text into numbers for search) | Ollama, running locally | Free, no API key. Embedding model to pick when building search (e.g. `nomic-embed-text`); vector size must match the Neo4j index |

## Rules

- FastAPI owns all MongoDB and Neo4j reads/writes; every write updates both stores through one function.
- Claude only judges and writes text. Fixed rules (IDs, status, links) live in code.
- IDs: `KEEP-D-001`, `KEEP-P-001`, `KEEP-I-001`. Never `#` before a number.
- Reply commands: `keep: intentional — reason`, `keep: accidental`, `keep: discuss`.
- Comments signed `— KEEP`.
- n8n workflows are built by hand by the user. Never write or edit files in `workflows/`.
- Add pytest tests with every backend part.
- Before using a library, check its current version and API; do not guess.
- User is on Windows, PowerShell.

Full background in KEEP_CONTEXT.md.
