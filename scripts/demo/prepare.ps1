# Prepare the demo branches in keep-sandbox (pushes branches only; open the PRs live in the demo).
# Run:  powershell -ExecutionPolicy Bypass -File C:\Users\shiva\keep\scripts\demo\prepare.ps1
# Note: git prints progress on stderr, so errors are checked with $LASTEXITCODE, not ErrorActionPreference.
$sandbox = "C:\Users\shiva\keep-sandbox"
$files = Join-Path $PSScriptRoot "files"
$branches = [ordered]@{
    "demo-payment-retry"     = "Retry failed charges"
    "demo-orders-pagination" = "Add orders list endpoint"
    "demo-slugify"           = "Add slugify helper"
    "demo-refund-queue"      = "Queue refunds instead of calling the provider inline"
}

function Run($what) {
    & cmd /c "$what 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "Failed: $what" }
}

if (-not (Test-Path $sandbox)) { Run "git clone https://github.com/shivawork-arch/keep-sandbox.git `"$sandbox`"" }
Push-Location $sandbox
try {
    Run "git checkout main"
    Run "git pull --ff-only"
    foreach ($branch in $branches.Keys) {
        Run "git checkout main"
        & cmd /c "git branch -D $branch 2>nul"   # fine if it did not exist
        Run "git checkout -b $branch"
        Copy-Item -Path (Join-Path $files "$branch\*") -Destination $sandbox -Recurse -Force
        Run "git add -A"
        Run "git commit -m `"$($branches[$branch])`""
        Run "git push -f -u origin $branch"
        Write-Host "pushed $branch" -ForegroundColor Green
    }
    Run "git checkout main"
} finally { Pop-Location }
Write-Host "`nBranches ready. Open the PRs live using scripts\demo\DEMO.md."
