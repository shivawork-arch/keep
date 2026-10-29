# KEEP demo runbook

About 10 minutes. Run every step once the day before.

## Before class

1. `powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\start.ps1`
2. Put the printed URL in keep-sandbox → Settings → Webhooks → Payload URL.
3. Reset memory to the seed: `cd C:\Users\shiva\keep\backend; python reset_memory.py ..\scripts\seed.json`
4. `powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\demo\prepare.ps1`
5. Close old PRs in keep-sandbox so only the demo ones show.
6. `cd C:\Users\shiva\keep\dashboard; npm run dev`, then open http://localhost:3000.
7. Check: `curl.exe http://localhost:8000/health` shows all ok, and the dashboard shows 11 decisions.

## 1. The memory (dashboard, Map)

Show the map: files on the left, people on the right, the 11 decisions between them.
Click **KEEP-D-001**: the decision, what was rejected, why, who decided, the links to D-006 and D-011.

## 2. A PR that breaks old decisions

On GitHub, open a PR from **demo-payment-retry**.
- Title: `Retry failed charges`
- Description: `Charges fail when the payment provider blips. This retries them so we stop losing orders.`

Expected KEEP comment (20 to 40 s): conflicts with D-001 (exponential backoff), D-006 (no idempotency key),
D-005 (float money), D-011 (no timeout), plus pattern rules P-001 (`Optional`) and P-002 (`print`).
One Claude call covers all of it.

Then reply on the PR: `keep: accidental` → "KEEP-D-001… stands".

## 3. Same idea, different words, different file

Open a PR from **demo-orders-pagination**.
- Title: `Add orders list endpoint`
- Description: `Lists orders newest first, 50 per page.`

`src/api/orders.py` is not governed by any decision. KEEP still flags D-007 (cursor pagination),
found by meaning alone. This is the weak spot of path-only lookup, fixed.

## 4. A clean PR costs nothing

Open a PR from **demo-slugify** (title `Add slugify helper`).
Expected: "Nothing in memory touches this change. Skipped the deep review." No Claude call.
Show the dashboard title block: the Claude call count did not move.

## 5. KEEP learns from a merge

Open a PR from **demo-refund-queue**.
- Title: `Queue refunds instead of calling the provider inline`
- Description:
  `Refunds now go through the background queue. We tried calling the provider inline, but its 30 second
  latency tied up web workers during the refund rush. Rule: anything that calls the payment provider for
  refunds runs in the queue worker, never in the request.`

Merge it. Expected comment: "KEEP recorded 1 decision… KEEP-D-012". Refresh the dashboard: D-012 appears on the map.

## 6. Before anyone writes code

Open an issue:
- Title: `Export orders to CSV`
- Body: `Add an endpoint that returns all orders as CSV, page by page with ?page=N. Keep the last export in memory for 10 minutes so repeat downloads are fast.`

Expected KEEP pre-mortem: D-007 (pagination) and D-002 (in-memory cache), with what to do instead and a checklist.

## 7. An incident

Open an issue:
- Title: `Customers charged twice on 30 Oct`
- Body: `During the payment provider timeouts around 14:05, some customers were charged twice. Retries look involved.`

Add the label `incident`. Expected: "recorded as KEEP-I-001", linked to D-006 and D-001.
On the dashboard the incident appears as a red diamond linked to both.

## 8. Ask

In the dashboard box: `Why do charges need an idempotency key?`
The answer cites D-006 and the cited decisions light up on the map.

## If something fails

- No KEEP comment: GitHub → Webhooks → Recent Deliveries. Red = tunnel died → rerun `scripts\tunnel.ps1`, update the URL, Redeliver.
- Dashboard says it can't reach the API: uvicorn is not running.
- Anything else: call the agent directly, e.g.
  `Invoke-RestMethod -Method Post -Uri http://localhost:8000/agents/pr-review -ContentType "application/json" -Body '{"repo":"shivawork-arch/keep-sandbox","number":N,"post":false}'`
