# Start a Cloudflare quick tunnel to FastAPI (the GitHub front door) and check it works.
# GitHub -> tunnel -> FastAPI /github/webhook (signature check) -> n8n on localhost.
# Run:  powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\tunnel.ps1
# Needs uvicorn running on port 8000.

$ErrorActionPreference = "Stop"
$log = Join-Path $env:TEMP "keep-tunnel.log"

try { Invoke-RestMethod "http://localhost:8000/health" -TimeoutSec 3 | Out-Null }
catch { throw "FastAPI is not running on :8000. Start uvicorn first." }

# Stop any old tunnel so only one runs.
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $log) { Remove-Item $log }

# Find cloudflared even if this window's PATH is out of date.
$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) {
    $cf = @(
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "$env:ProgramFiles\cloudflared\cloudflared.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $cf) { throw "cloudflared not found. Install: winget install --id Cloudflare.cloudflared -e" }

Write-Host "Starting tunnel..."
Start-Process $cf -ArgumentList "tunnel --url http://localhost:8000" `
    -RedirectStandardError $log -WindowStyle Hidden

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

Write-Host "Waiting for the tunnel to answer..."
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    try {
        Invoke-RestMethod "$url/health" -TimeoutSec 5 | Out-Null
        Write-Host ""
        Write-Host "OK. Set this as the GitHub webhook Payload URL:"
        Write-Host "  $url/github/webhook"
        exit 0
    } catch { }
}
throw "Tunnel started but $url/health did not answer in 2 minutes. See $log"
