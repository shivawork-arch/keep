# Start a Cloudflare quick tunnel to n8n, put its URL in .env, restart n8n, check it works.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\tunnel.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root ".env"
$log = Join-Path $env:TEMP "keep-tunnel.log"

# Stop any old tunnel so only one runs.
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $log) { Remove-Item $log }

Write-Host "Starting tunnel..."
Start-Process cloudflared -ArgumentList "tunnel --url http://localhost:5678" `
    -RedirectStandardError $log -WindowStyle Hidden

# cloudflared prints the URL within a few seconds.
$url = $null
for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
if (-not $url) { throw "No tunnel URL after 30s. See $log" }
Write-Host "Tunnel: $url"

# Replace (or add) N8N_WEBHOOK_URL in .env.
$lines = @(Get-Content $envFile | Where-Object { $_ -notmatch '^N8N_WEBHOOK_URL=' })
$lines += "N8N_WEBHOOK_URL=$url/"
Set-Content -Path $envFile -Value $lines

Push-Location $root
docker compose up -d n8n | Out-Null
Pop-Location

# New addresses take a little while to resolve.
Write-Host "Waiting for the tunnel to answer..."
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-RestMethod "$url/healthz" -TimeoutSec 5
        if ($r.status -eq "ok") {
            Write-Host "OK: n8n reachable at $url"
            Write-Host "GitHub webhook URLs start with: $url/webhook/"
            exit 0
        }
    } catch { }
}
throw "Tunnel started but $url/healthz did not answer in 2 minutes. See $log"
