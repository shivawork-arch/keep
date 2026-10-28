# Bring all of KEEP up after a reboot or sleep: Docker services, FastAPI, tunnel.
# Run:  powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\start.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

# 1. Docker Desktop
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep 3; docker info *> $null; if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) { throw "Docker did not start in 3 minutes" }
}

# 2. Containers
Push-Location $root
docker compose up -d
Pop-Location

# 3. FastAPI in its own window (skip if already running)
$up = $false
try { Invoke-RestMethod http://localhost:8000/health -TimeoutSec 2 | Out-Null; $up = $true } catch { }
if (-not $up) {
    Write-Host "Starting FastAPI in a new window..."
    $backend = Join-Path $root "backend"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$backend'; .\.venv\Scripts\Activate.ps1; uvicorn app.main:app --reload"
}

# 4. Wait until all three services report ok (Neo4j is the slow one)
Write-Host "Waiting for MongoDB, Neo4j and Ollama..."
$h = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep 3
    try {
        $h = Invoke-RestMethod http://localhost:8000/health -TimeoutSec 3
        if ($h.mongodb -eq "ok" -and $h.neo4j -eq "ok" -and $h.ollama -eq "ok") { break }
    } catch { }
}
Write-Host ("health: mongodb={0} neo4j={1} ollama={2}" -f $h.mongodb, $h.neo4j, $h.ollama)

# 5. Tunnel (prints the GitHub Payload URL)
& (Join-Path $PSScriptRoot "tunnel.ps1")
