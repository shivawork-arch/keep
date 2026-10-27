# Send one fake GitHub event per route (plus ones that should be dropped) to the n8n router.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\send_test_events.ps1 [-Url http://localhost:5678/webhook/github]
param([string]$Url = "http://localhost:5678/webhook/github")

$repo = '"repository":{"full_name":"shivawork-arch/keep-sandbox"}'
$user = '"sender":{"type":"User"}'

$events = @(
    @{ expect = "PR Review";          event = "pull_request";  body = "{`"action`":`"opened`",`"pull_request`":{`"number`":1,`"merged`":false},$repo,$user}" },
    @{ expect = "Decision Extractor"; event = "pull_request";  body = "{`"action`":`"closed`",`"pull_request`":{`"number`":2,`"merged`":true},$repo,$user}" },
    @{ expect = "Pre-Mortem";         event = "issues";        body = "{`"action`":`"opened`",`"issue`":{`"number`":3},$repo,$user}" },
    @{ expect = "Reply Handler";      event = "issue_comment"; body = "{`"action`":`"created`",`"issue`":{`"number`":4},`"comment`":{`"body`":`"keep: intentional - load test only`"},$repo,$user}" },
    @{ expect = "Commit Keeper";      event = "push";          body = "{`"ref`":`"refs/heads/main`",$repo,$user}" },
    @{ expect = "(dropped: closed, not merged)"; event = "pull_request"; body = "{`"action`":`"closed`",`"pull_request`":{`"number`":5,`"merged`":false},$repo,$user}" },
    @{ expect = "(dropped: bot)";     event = "issues";        body = "{`"action`":`"opened`",`"issue`":{`"number`":6},$repo,`"sender`":{`"type`":`"Bot`"}}" }
)

foreach ($e in $events) {
    try {
        Invoke-RestMethod -Method Post -Uri $Url -ContentType "application/json" `
            -Headers @{ "X-GitHub-Event" = $e.event } -Body $e.body | Out-Null
        Write-Host ("sent  {0,-14} expect: {1}" -f $e.event, $e.expect)
    } catch {
        Write-Host ("FAIL  {0,-14} {1}" -f $e.event, $_.Exception.Message)
    }
    Start-Sleep -Milliseconds 300
}
Write-Host "`nNow open n8n > Executions and check each run ended at the expected node."
